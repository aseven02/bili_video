"""根据 video_batches JSON 并发抓取作者资料和视频详情。

输入：[{"uid": "8542312", "videos": ["BV...", "BV..."]}]
输出：每位作者一个 JSON；作者完成后原子写盘，重复运行会跳过已成功项目。
这里只调用无需 Cookie 和 WBI 签名的公开 card/view 接口。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import random
import re
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx


BILI_API = "https://api.bilibili.com"
SCHEMA_VERSION = 1
BVID_PATTERN = re.compile(r"^BV[0-9A-Za-z]{10}$")
RISK_HTTP_CODES = {403, 412, 429}
RISK_API_CODES = {-352, -412, -509}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
LOG = logging.getLogger("bili_batch")


@dataclass(frozen=True)
class Config:
    input_path: pathlib.Path
    output_dir: pathlib.Path
    workers: int
    retries: int
    retry_delay: float
    risk_delay: float
    timeout: float
    request_delay_min: float
    request_delay_max: float
    refresh: bool
    max_creators: int | None
    max_videos: int | None


@dataclass(frozen=True)
class CreatorTask:
    uid: str
    videos: tuple[str, ...]
    previous: dict[str, Any] | None = None


class StopCrawl(Exception):
    pass


class ApiError(RuntimeError):
    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail
        super().__init__(detail["message"])


class RiskCooldown:
    """任何线程命中风控后，所有线程一起暂停。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._until = 0.0

    def trigger(self, seconds: float) -> None:
        with self._lock:
            self._until = max(self._until, time.monotonic() + seconds)

    def wait(self, stopped: threading.Event) -> None:
        while True:
            with self._lock:
                delay = self._until - time.monotonic()
            if delay <= 0:
                return
            if stopped.wait(min(delay, 1.0)):
                raise StopCrawl


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_config() -> Config:
    parser = argparse.ArgumentParser(description="批量抓取 Bilibili 作者资料和视频详情")
    parser.add_argument("--input", "-i", required=True, help="video_batches JSON 文件")
    parser.add_argument("--output-dir", "-o", default="", help="默认 video_details/<输入文件名>")
    parser.add_argument("--workers", type=int, default=8, help="作者并发线程数，默认 8")
    parser.add_argument("--retries", type=int, default=3, help="每次请求最大尝试次数")
    parser.add_argument("--retry-delay", type=float, default=2.0, help="首次重试等待秒数")
    parser.add_argument("--risk-delay", type=float, default=60.0, help="风控全局冷却秒数")
    parser.add_argument("--timeout", type=float, default=15.0, help="请求超时秒数")
    parser.add_argument("--request-delay-min", type=float, default=0.5, help="请求最短间隔")
    parser.add_argument("--request-delay-max", type=float, default=1.0, help="请求最长间隔")
    parser.add_argument("--refresh", action="store_true", help="忽略断点，刷新所有数据")
    parser.add_argument("--max-creators", type=int, help="试跑：最多处理 N 位作者")
    parser.add_argument("--max-videos", type=int, help="试跑：每位作者最多处理 N 个视频")
    args = parser.parse_args()

    input_path = pathlib.Path(args.input).expanduser()
    if not input_path.is_file():
        parser.error(f"输入文件不存在: {input_path}")
    for name, value in {
        "--workers": args.workers,
        "--retries": args.retries,
        "--timeout": args.timeout,
    }.items():
        if value <= 0:
            parser.error(f"{name} 必须大于 0")
    if args.retry_delay < 0 or args.risk_delay < 0:
        parser.error("重试等待时间不能为负数")
    if args.request_delay_min < 0 or args.request_delay_min > args.request_delay_max:
        parser.error("请求间隔必须非负，且 min 不能大于 max")
    if args.max_creators is not None and args.max_creators <= 0:
        parser.error("--max-creators 必须大于 0")
    if args.max_videos is not None and args.max_videos <= 0:
        parser.error("--max-videos 必须大于 0")

    output_dir = (
        pathlib.Path(args.output_dir).expanduser()
        if args.output_dir
        else pathlib.Path("video_batches_result") / input_path.stem
    )
    return Config(
        input_path=input_path.resolve(),
        output_dir=output_dir.resolve(),
        workers=args.workers,
        retries=args.retries,
        retry_delay=args.retry_delay,
        risk_delay=args.risk_delay,
        timeout=args.timeout,
        request_delay_min=args.request_delay_min,
        request_delay_max=args.request_delay_max,
        refresh=args.refresh,
        max_creators=args.max_creators,
        max_videos=args.max_videos,
    )


def load_tasks(path: pathlib.Path) -> tuple[list[CreatorTask], dict[str, int]]:
    with path.open(encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, list):
        raise ValueError("输入 JSON 根节点必须是数组")

    grouped: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    duplicate_uids = duplicate_videos = 0
    for index, item in enumerate(payload, 1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个作者必须是对象")
        raw_uid = item.get("uid")
        uid = "" if isinstance(raw_uid, bool) else str(raw_uid or "").strip()
        if not uid.isdigit():
            raise ValueError(f"第 {index} 个作者缺少有效数字 uid")
        if not isinstance(item.get("videos"), list):
            raise ValueError(f"UID {uid} 的 videos 必须是数组")
        if uid in grouped:
            duplicate_uids += 1
        else:
            grouped[uid], seen[uid] = [], set()
        for video_index, raw_bvid in enumerate(item["videos"], 1):
            bvid = raw_bvid.strip() if isinstance(raw_bvid, str) else ""
            if not BVID_PATTERN.fullmatch(bvid):
                raise ValueError(f"UID {uid} 的第 {video_index} 个 BV 号无效: {raw_bvid!r}")
            if bvid in seen[uid]:
                duplicate_videos += 1
                continue
            seen[uid].add(bvid)
            grouped[uid].append(bvid)

    tasks = [CreatorTask(uid, tuple(videos)) for uid, videos in grouped.items()]
    first_owner: dict[str, str] = {}
    cross_author: set[str] = set()
    for task in tasks:
        for bvid in task.videos:
            first_uid = first_owner.setdefault(bvid, task.uid)
            if first_uid != task.uid:
                cross_author.add(bvid)
    return tasks, {
        "input_creators": len(payload),
        "unique_creators": len(tasks),
        "videos": sum(len(task.videos) for task in tasks),
        "duplicate_uids": duplicate_uids,
        "duplicate_videos": duplicate_videos,
        "cross_author_bvids": len(cross_author),
    }


def atomic_write(path: pathlib.Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = pathlib.Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        temp_path.replace(path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def read_result(path: pathlib.Path, uid: str) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        result = json.load(file)
    if not isinstance(result, dict) or result.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"无法识别已有结果: {path}")
    if str(result.get("uid")) != uid or not isinstance(result.get("videos"), list):
        raise ValueError(f"已有结果结构异常: {path}")
    return result


def complete(result: dict[str, Any], task: CreatorTask) -> bool:
    videos = result.get("videos") or []
    return (
        result.get("status") == "success"
        and isinstance(result.get("author"), dict)
        and [item.get("bvid") for item in videos] == list(task.videos)
        and all(item.get("status") == "success" for item in videos)
    )


def error_info(
    kind: str,
    message: str,
    attempts: int = 1,
    *,
    http_status: int | None = None,
    api_code: int | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "message": message,
        "attempts": attempts,
        "http_status": http_status,
        "api_code": api_code,
        "time": now(),
    }


class Requester:
    def __init__(
        self,
        client: httpx.Client,
        config: Config,
        cooldown: RiskCooldown,
        stopped: threading.Event,
    ) -> None:
        self.client, self.config = client, config
        self.cooldown, self.stopped = cooldown, stopped
        self.last_request = 0.0

    def sleep(self, seconds: float) -> None:
        if seconds > 0 and self.stopped.wait(seconds):
            raise StopCrawl

    def before_request(self) -> None:
        if self.stopped.is_set():
            raise StopCrawl
        if self.last_request:
            interval = random.uniform(self.config.request_delay_min, self.config.request_delay_max)
            self.sleep(max(0.0, interval - (time.monotonic() - self.last_request)))
        self.cooldown.wait(self.stopped)
        self.last_request = time.monotonic()

    def get(self, path: str, params: dict[str, Any], referer: str, label: str) -> dict[str, Any]:
        last_error: dict[str, Any] | None = None
        for attempt in range(1, self.config.retries + 1):
            self.before_request()
            retryable = risk = False
            retry_after = 0.0
            try:
                response = self.client.get(
                    BILI_API + path,
                    params=params,
                    headers={"Referer": referer},
                )
                try:
                    retry_after = float(response.headers.get("Retry-After", 0))
                except ValueError:
                    retry_after = 0.0
                if response.status_code >= 400:
                    risk = response.status_code in RISK_HTTP_CODES
                    retryable = risk or response.status_code == 408 or response.status_code >= 500
                    last_error = error_info(
                        "http", f"HTTP {response.status_code}", attempt,
                        http_status=response.status_code,
                    )
                else:
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ValueError("响应根节点不是对象")
                    code = payload.get("code")
                    if code == 0:
                        data = payload.get("data") or {}
                        if not isinstance(data, dict):
                            raise ValueError("data 字段不是对象")
                        return data
                    risk = code in RISK_API_CODES
                    retryable = risk
                    last_error = error_info(
                        "api", f"API code={code}, message={payload.get('message')}", attempt,
                        api_code=code if isinstance(code, int) else None,
                    )
            except httpx.TimeoutException as error:
                retryable = True
                last_error = error_info("timeout", str(error) or "请求超时", attempt)
            except httpx.TransportError as error:
                retryable = True
                last_error = error_info("network", str(error), attempt)
            except (json.JSONDecodeError, ValueError) as error:
                retryable = True
                last_error = error_info("response", str(error), attempt)

            if risk:
                self.cooldown.trigger(max(self.config.risk_delay, retry_after))
            if not retryable or attempt == self.config.retries:
                raise ApiError(last_error or error_info("unknown", f"{label} 请求失败", attempt))
            delay = max(retry_after, self.config.retry_delay * 2 ** (attempt - 1))
            if risk:
                delay = max(delay, self.config.risk_delay)
            delay += random.uniform(0, delay * 0.25) if delay else 0
            LOG.warning("%s 失败（%d/%d），%.1f 秒后重试", label, attempt, self.config.retries, delay)
            self.sleep(delay)
        raise AssertionError("unreachable")


def compact_author(uid: str, data: dict[str, Any]) -> dict[str, Any]:
    card = data.get("card")
    if not isinstance(card, dict) or not card:
        raise ValueError("作者接口未返回有效 card")
    official = card.get("Official") or card.get("official") or card.get("official_verify") or {}
    level = card.get("level_info") or {}
    actual_uid = str(card.get("mid") or uid)
    return {
        "uid": actual_uid,
        "name": card.get("name"),
        "face": card.get("face"),
        "fans": data.get("follower") if data.get("follower") is not None else card.get("fans"),
        "like_num": data.get("like_num"),
        "level": level.get("current_level"),
        "sign": card.get("sign"),
        "description": card.get("description") or card.get("sign"),
        "official": {
            "type": official.get("type") if official.get("type") is not None else official.get("role"),
            "title": official.get("title"),
            "description": official.get("desc") or official.get("description"),
        },
        "archive_count": data.get("archive_count"),
        "article_count": data.get("article_count"),
        "space_url": f"https://space.bilibili.com/{actual_uid}",
    }


def compact_video(uid: str, bvid: str, data: dict[str, Any]) -> dict[str, Any]:
    if not data.get("bvid") and not data.get("title"):
        raise ValueError("视频接口未返回有效详情")
    owner = data.get("owner") or {}
    rights = data.get("rights") or {}
    staff = [
        {key: item.get(key) for key in ("mid", "title", "name", "face", "follower")}
        for item in data.get("staff") or []
        if isinstance(item, dict)
    ]
    owner_uid = str(owner.get("mid") or "")
    relation = (
        "owner" if owner_uid == uid
        else "staff" if any(str(item.get("mid") or "") == uid for item in staff)
        else "unverified"
    )
    return {
        "bvid": str(data.get("bvid") or bvid),
        "status": "success",
        "error": None,
        "fetched_at": now(),
        "creator_relation": relation,
        "title": data.get("title"),
        "pic": data.get("pic"),
        "pubdate": data.get("pubdate"),
        "desc": data.get("desc"),
        "duration": data.get("duration"),
        "owner": owner,
        "stat": data.get("stat") or {},
        "argue_info": data.get("argue_info") or {},
        "is_cooperation": rights.get("is_cooperation"),
        "staff": staff,
    }


def as_error(error: Exception, kind: str = "parse") -> dict[str, Any]:
    return error.detail if isinstance(error, ApiError) else error_info(kind, str(error))


def crawl_creator(
    task: CreatorTask,
    config: Config,
    cooldown: RiskCooldown,
    stopped: threading.Event,
) -> dict[str, Any]:
    previous = task.previous or {}
    started_at = now()
    author = previous.get("author") if isinstance(previous.get("author"), dict) else None
    previous_videos = {
        item["bvid"]: item
        for item in previous.get("videos") or []
        if isinstance(item, dict) and item.get("bvid") and item.get("status") == "success"
    }
    author_error = None
    videos: list[dict[str, Any]] = []

    with httpx.Client(headers=HEADERS, timeout=config.timeout, follow_redirects=True) as client:
        requester = Requester(client, config, cooldown, stopped)
        if author is None:
            try:
                author = compact_author(
                    task.uid,
                    requester.get(
                        "/x/web-interface/card",
                        {"mid": task.uid, "photo": "true"},
                        f"https://space.bilibili.com/{task.uid}",
                        f"UID {task.uid} 作者",
                    ),
                )
            except StopCrawl:
                raise
            except Exception as error:
                author_error = as_error(error)

        for bvid in task.videos:
            if stopped.is_set():
                raise StopCrawl
            if bvid in previous_videos:
                videos.append(previous_videos[bvid])
                continue
            try:
                data = requester.get(
                    "/x/web-interface/view",
                    {"bvid": bvid},
                    f"https://www.bilibili.com/video/{bvid}",
                    f"UID {task.uid} 视频 {bvid}",
                )
                videos.append(compact_video(task.uid, bvid, data))
            except StopCrawl:
                raise
            except Exception as error:
                videos.append({"bvid": bvid, "status": "failed", "error": as_error(error)})

    success_count = sum(video["status"] == "success" for video in videos)
    failed_count = len(videos) - success_count
    if author is not None and failed_count == 0:
        status = "success"
    elif author is not None or success_count:
        status = "partial_failed"
    else:
        status = "failed"
    return {
        "schema_version": SCHEMA_VERSION,
        "uid": task.uid,
        "status": status,
        "author": author,
        "author_error": author_error,
        "videos": videos,
        "summary": {
            "video_total": len(videos),
            "video_success": success_count,
            "video_failed": failed_count,
        },
        "started_at": started_at,
        "finished_at": now(),
    }


def failed_worker_result(task: CreatorTask, error: Exception) -> dict[str, Any]:
    detail = as_error(error, "worker")
    return {
        "schema_version": SCHEMA_VERSION,
        "uid": task.uid,
        "status": "failed",
        "author": None,
        "author_error": detail,
        "videos": [{"bvid": bvid, "status": "failed", "error": detail} for bvid in task.videos],
        "summary": {"video_total": len(task.videos), "video_success": 0, "video_failed": len(task.videos)},
        "started_at": now(),
        "finished_at": now(),
    }


def write_manifest(
    path: pathlib.Path,
    config: Config,
    validation: dict[str, int],
    tasks: list[CreatorTask],
    statuses: dict[str, str],
    state: str,
) -> None:
    counts = Counter(statuses.values())
    atomic_write(path, {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now(),
        "input": {"path": str(config.input_path), **validation},
        "selected": {
            "creators": len(tasks),
            "videos": sum(len(task.videos) for task in tasks),
        },
        "config": {
            "workers": config.workers,
            "retries": config.retries,
            "retry_delay": config.retry_delay,
            "risk_delay": config.risk_delay,
            "timeout": config.timeout,
            "request_delay": [config.request_delay_min, config.request_delay_max],
        },
        "run": {
            "state": state,
            "completed": len(statuses),
            "remaining": len(tasks) - len(statuses),
            "success": counts["success"],
            "partial_failed": counts["partial_failed"],
            "failed": counts["failed"],
        },
    })


def run(config: Config) -> int:
    tasks, validation = load_tasks(config.input_path)
    if config.max_creators:
        tasks = tasks[:config.max_creators]
    if config.max_videos:
        tasks = [CreatorTask(task.uid, task.videos[:config.max_videos]) for task in tasks]
    if not tasks:
        raise ValueError("输入中没有可处理的作者")

    creators_dir = config.output_dir / "creators"
    creators_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = config.output_dir / "manifest.json"
    if manifest_path.exists():
        with manifest_path.open(encoding="utf-8") as file:
            old_manifest = json.load(file)
        old_input = str((old_manifest.get("input") or {}).get("path") or "")
        if old_input and old_input != str(config.input_path):
            raise ValueError(f"输出目录已属于另一个输入文件: {old_input}")
    pending: list[CreatorTask] = []
    statuses: dict[str, str] = {}
    for task in tasks:
        path = creators_dir / f"{task.uid}.json"
        previous = read_result(path, task.uid) if path.exists() and not config.refresh else None
        if previous and complete(previous, task):
            statuses[task.uid] = "success"
        else:
            pending.append(CreatorTask(task.uid, task.videos, previous))

    LOG.info(
        "共 %d 位作者、%d 个视频；断点跳过 %d 位，待处理 %d 位",
        len(tasks), sum(len(task.videos) for task in tasks), len(statuses), len(pending),
    )
    if validation["duplicate_uids"] or validation["duplicate_videos"]:
        LOG.warning(
            "输入已合并 %d 个重复 UID，移除 %d 个作者内重复视频",
            validation["duplicate_uids"], validation["duplicate_videos"],
        )
    if validation["cross_author_bvids"]:
        LOG.info("保留 %d 个跨作者重复 BV 号", validation["cross_author_bvids"])
    write_manifest(manifest_path, config, validation, tasks, statuses, "running")

    stopped, cooldown = threading.Event(), RiskCooldown()
    executor = ThreadPoolExecutor(max_workers=config.workers, thread_name_prefix="bili-author")
    futures: dict[Future[dict[str, Any]], CreatorTask] = {
        executor.submit(crawl_creator, task, config, cooldown, stopped): task for task in pending
    }
    try:
        while futures:
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                task = futures.pop(future)
                try:
                    result = future.result()
                except StopCrawl:
                    continue
                except Exception as error:
                    LOG.exception("UID %s 工作线程异常", task.uid)
                    result = failed_worker_result(task, error)
                atomic_write(creators_dir / f"{task.uid}.json", result)
                statuses[task.uid] = result["status"]
                write_manifest(manifest_path, config, validation, tasks, statuses, "running")
                summary = result["summary"]
                LOG.info(
                    "[%d/%d] UID %s %s，视频成功 %d/%d",
                    len(statuses), len(tasks), task.uid, result["status"],
                    summary["video_success"], summary["video_total"],
                )
    except KeyboardInterrupt:
        LOG.warning("收到中断信号，正在停止当前任务……")
        stopped.set()
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        write_manifest(manifest_path, config, validation, tasks, statuses, "interrupted")
        LOG.warning("已保存 %d/%d 位作者，使用相同命令可继续", len(statuses), len(tasks))
        return 130
    else:
        executor.shutdown(wait=True)

    counts = Counter(statuses.values())
    state = "success" if counts["success"] == len(tasks) else "completed_with_errors"
    write_manifest(manifest_path, config, validation, tasks, statuses, state)
    LOG.info(
        "完成：成功 %d，部分失败 %d，失败 %d；输出 %s",
        counts["success"], counts["partial_failed"], counts["failed"], config.output_dir,
    )
    return 0 if state == "success" else 2


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        return run(parse_config())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        LOG.error("任务无法启动：%s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
