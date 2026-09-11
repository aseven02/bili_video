"""从 video_batches_result 下载 Bilibili 视频封面。

封面按 BVID 全局去重并散列到子目录；SQLite 保存下载状态。
图片先写入同目录临时文件，校验成功后再原子替换正式文件。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import pathlib
import random
import re
import sqlite3
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import urlsplit

import httpx


BVID_PATTERN = re.compile(r"^BV[0-9A-Za-z]{10}$")
RISK_HTTP_CODES = {403, 412, 429}
RETRY_HTTP_CODES = {408, 425}
CONTENT_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}
URL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Referer": "https://www.bilibili.com/",
}
LOG = logging.getLogger("bili-cover")


@dataclass(frozen=True)
class Config:
    input_dir: pathlib.Path
    assets_dir: pathlib.Path
    covers_dir: pathlib.Path
    database: pathlib.Path
    workers: int
    retries: int
    retry_delay: float
    risk_delay: float
    timeout: float
    request_delay_min: float
    request_delay_max: float
    min_bytes: int
    max_bytes: int
    max_videos: int | None
    refresh: bool
    scan_only: bool


@dataclass(frozen=True)
class CoverTask:
    bvid: str
    source_url: str
    local_path: str


@dataclass(frozen=True)
class DownloadResult:
    bvid: str
    status: str
    local_path: str | None
    content_type: str | None
    size_bytes: int | None
    attempts: int
    http_status: int | None
    error: str | None


class StopDownload(Exception):
    pass


class RiskCooldown:
    """一个线程遇到明确风控响应时，让全部下载线程共同冷却。"""

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
                raise StopDownload


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_args() -> Config:
    parser = argparse.ArgumentParser(description="批量下载 video_batches_result 中的视频封面")
    parser.add_argument("--input-dir", default="video_batches_result", help="结果根目录或单个批次目录")
    parser.add_argument("--assets-dir", help="资源目录，默认 <input-dir>/assets")
    parser.add_argument("--database", help="SQLite 路径，默认 <assets-dir>/cover_manifest.sqlite3")
    parser.add_argument("--workers", type=int, default=6, help="下载线程数，默认 6")
    parser.add_argument("--retries", type=int, default=3, help="每张封面最大尝试次数，默认 3")
    parser.add_argument("--retry-delay", type=float, default=1.0, help="首次重试等待秒数")
    parser.add_argument("--risk-delay", type=float, default=60.0, help="403/412/429 全局冷却秒数")
    parser.add_argument("--timeout", type=float, default=20.0, help="单次请求超时秒数")
    parser.add_argument("--request-delay-min", type=float, default=0.05, help="每个线程请求前最短等待")
    parser.add_argument("--request-delay-max", type=float, default=0.20, help="每个线程请求前最长等待")
    parser.add_argument("--min-bytes", type=int, default=128, help="有效图片最小字节数")
    parser.add_argument("--max-mb", type=float, default=30.0, help="单张图片最大 MB")
    parser.add_argument("--max-videos", type=int, help="试跑：本次最多处理 N 个待下载 BVID")
    parser.add_argument("--refresh", action="store_true", help="重新下载已有成功文件")
    parser.add_argument("--scan-only", action="store_true", help="只扫描 JSON 并更新 SQLite，不下载")
    parser.add_argument("--verbose", action="store_true", help="显示更详细日志")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.DEBUG if args.verbose else logging.WARNING)
    input_dir = pathlib.Path(args.input_dir).expanduser().resolve()
    if not input_dir.is_dir():
        parser.error(f"输入目录不存在: {input_dir}")
    assets_dir = (
        pathlib.Path(args.assets_dir).expanduser().resolve()
        if args.assets_dir
        else input_dir / "assets"
    )
    database = (
        pathlib.Path(args.database).expanduser().resolve()
        if args.database
        else assets_dir / "cover_manifest.sqlite3"
    )
    for name, value in {
        "--workers": args.workers,
        "--retries": args.retries,
        "--timeout": args.timeout,
        "--min-bytes": args.min_bytes,
        "--max-mb": args.max_mb,
    }.items():
        if value <= 0:
            parser.error(f"{name} 必须大于 0")
    if args.retry_delay < 0 or args.risk_delay < 0:
        parser.error("重试和风控等待时间不能为负数")
    if args.request_delay_min < 0 or args.request_delay_min > args.request_delay_max:
        parser.error("请求间隔必须非负，且 min 不能大于 max")
    if args.max_videos is not None and args.max_videos <= 0:
        parser.error("--max-videos 必须大于 0")
    return Config(
        input_dir=input_dir,
        assets_dir=assets_dir,
        covers_dir=assets_dir / "covers",
        database=database,
        workers=args.workers,
        retries=args.retries,
        retry_delay=args.retry_delay,
        risk_delay=args.risk_delay,
        timeout=args.timeout,
        request_delay_min=args.request_delay_min,
        request_delay_max=args.request_delay_max,
        min_bytes=args.min_bytes,
        max_bytes=int(args.max_mb * 1024 * 1024),
        max_videos=args.max_videos,
        refresh=args.refresh,
        scan_only=args.scan_only,
    )


def discover_creator_files(root: pathlib.Path) -> list[pathlib.Path]:
    if (root / "creators").is_dir():
        files = sorted((root / "creators").glob("*.json"))
    else:
        files = sorted(root.glob("video_batches_*/creators/*.json"))
    if not files:
        raise FileNotFoundError(f"没有找到作者结果 JSON: {root}")
    return files


def connect_database(path: pathlib.Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS covers (
            bvid TEXT PRIMARY KEY,
            source_url TEXT NOT NULL,
            local_path TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            content_type TEXT,
            size_bytes INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            http_status INTEGER,
            error TEXT,
            discovered_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_covers_status ON covers(status)")
    connection.commit()
    return connection


def normalize_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("http://"):
        url = "https://" + url.removeprefix("http://")
    parsed = urlsplit(url)
    return url if parsed.scheme == "https" and parsed.netloc else None


def extension_from_url(url: str) -> str:
    extension = pathlib.PurePosixPath(urlsplit(url).path).suffix.lower()
    if extension == ".jpeg":
        return ".jpg"
    return extension if extension in URL_EXTENSIONS else ".jpg"


def relative_cover_path(bvid: str, extension: str) -> str:
    bucket = hashlib.sha256(bvid.encode("ascii")).hexdigest()[:2]
    return f"{bucket}/{bvid}{extension}"


def scan_results(
    connection: sqlite3.Connection,
    files: list[pathlib.Path],
) -> dict[str, int]:
    scanned_videos = valid_covers = invalid_files = invalid_records = 0
    timestamp = now()
    sql = """
        INSERT INTO covers (bvid, source_url, local_path, discovered_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(bvid) DO UPDATE SET source_url = excluded.source_url
    """
    rows: list[tuple[str, str, str, str, str]] = []
    for index, path in enumerate(files, 1):
        try:
            with path.open(encoding="utf-8") as file:
                result = json.load(file)
            videos = result.get("videos") if isinstance(result, dict) else None
            if not isinstance(videos, list):
                raise ValueError("缺少 videos 数组")
        except (OSError, ValueError, json.JSONDecodeError) as error:
            invalid_files += 1
            LOG.warning("跳过无效文件 %s: %s", path, error)
            continue

        for video in videos:
            scanned_videos += 1
            if not isinstance(video, dict):
                invalid_records += 1
                continue
            bvid = str(video.get("bvid") or "").strip()
            source_url = normalize_url(video.get("pic"))
            if not BVID_PATTERN.fullmatch(bvid) or source_url is None:
                invalid_records += 1
                continue
            local_path = relative_cover_path(bvid, extension_from_url(source_url))
            rows.append((bvid, source_url, local_path, timestamp, timestamp))
            valid_covers += 1
            if len(rows) >= 2_000:
                connection.executemany(sql, rows)
                connection.commit()
                rows.clear()
        if index % 500 == 0:
            LOG.info("已扫描作者文件 %s/%s", f"{index:,}", f"{len(files):,}")
    if rows:
        connection.executemany(sql, rows)
        connection.commit()
    unique_covers = connection.execute("SELECT COUNT(*) FROM covers").fetchone()[0]
    return {
        "creator_files": len(files),
        "scanned_videos": scanned_videos,
        "valid_cover_records": valid_covers,
        "unique_bvids": unique_covers,
        "duplicate_records": max(valid_covers - unique_covers, 0),
        "invalid_files": invalid_files,
        "invalid_records": invalid_records,
    }


def image_signature_valid(path: pathlib.Path, min_bytes: int) -> bool:
    try:
        if path.stat().st_size < min_bytes:
            return False
        with path.open("rb") as file:
            header = file.read(16)
    except OSError:
        return False
    return (
        header.startswith(b"\xff\xd8\xff")
        or header.startswith(b"\x89PNG\r\n\x1a\n")
        or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")
        or header.startswith((b"GIF87a", b"GIF89a"))
        or (len(header) >= 12 and header[4:8] == b"ftyp" and b"avif" in header[8:16])
    )


def pending_tasks(connection: sqlite3.Connection, config: Config) -> Iterator[CoverTask]:
    query = "SELECT bvid, source_url, local_path, status FROM covers ORDER BY bvid"
    yielded = 0
    cursor = connection.execute(query)
    while rows := cursor.fetchmany(2_000):
        for bvid, source_url, local_path, status in rows:
            path = config.covers_dir / local_path
            if not config.refresh and image_signature_valid(path, config.min_bytes):
                if status != "success":
                    connection.execute(
                        """UPDATE covers SET status='success', size_bytes=?, error=NULL,
                           updated_at=? WHERE bvid=?""",
                        (path.stat().st_size, now(), bvid),
                    )
                continue
            if not config.refresh and status == "success":
                connection.execute(
                    "UPDATE covers SET status='pending', error=?, updated_at=? WHERE bvid=?",
                    ("成功记录对应的文件缺失或无效", now(), bvid),
                )
            yield CoverTask(bvid, source_url, local_path)
            yielded += 1
            if config.max_videos is not None and yielded >= config.max_videos:
                connection.commit()
                return
    connection.commit()


def parse_retry_after(response: httpx.Response) -> float:
    try:
        return max(float(response.headers.get("Retry-After", "0")), 0.0)
    except ValueError:
        return 0.0


def download_cover(
    task: CoverTask,
    config: Config,
    client: httpx.Client,
    cooldown: RiskCooldown,
    stopped: threading.Event,
) -> DownloadResult:
    last_error: str | None = None
    last_http_status: int | None = None
    for attempt in range(1, config.retries + 1):
        temp_path: pathlib.Path | None = None
        retryable = True
        retry_after = 0.0
        try:
            cooldown.wait(stopped)
            if stopped.wait(random.uniform(config.request_delay_min, config.request_delay_max)):
                raise StopDownload
            with client.stream("GET", task.source_url) as response:
                last_http_status = response.status_code
                retry_after = parse_retry_after(response)
                if response.status_code != 200:
                    last_error = f"HTTP {response.status_code}"
                    retryable = (
                        response.status_code in RISK_HTTP_CODES
                        or response.status_code in RETRY_HTTP_CODES
                        or response.status_code >= 500
                    )
                    if response.status_code in RISK_HTTP_CODES:
                        cooldown.trigger(max(config.risk_delay, retry_after))
                    raise RuntimeError(last_error)

                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if not content_type.startswith("image/"):
                    last_error = f"非图片响应: {content_type or 'unknown'}"
                    raise RuntimeError(last_error)
                extension = CONTENT_EXTENSIONS.get(content_type, extension_from_url(task.source_url))
                local_path = relative_cover_path(task.bvid, extension)
                destination = config.covers_dir / local_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temp_name = tempfile.mkstemp(
                    prefix=f".{task.bvid}.", suffix=".part", dir=destination.parent
                )
                temp_path = pathlib.Path(temp_name)
                size = 0
                with os.fdopen(descriptor, "wb") as file:
                    for chunk in response.iter_bytes(64 * 1024):
                        size += len(chunk)
                        if size > config.max_bytes:
                            raise ValueError(f"图片超过限制 {config.max_bytes} bytes")
                        file.write(chunk)
                    file.flush()
                    os.fsync(file.fileno())
                if not image_signature_valid(temp_path, config.min_bytes):
                    raise ValueError("图片内容过小或文件签名无效")
                temp_path.replace(destination)
                temp_path = None
                old_path = config.covers_dir / task.local_path
                if old_path != destination and old_path.is_file():
                    try:
                        old_path.unlink()
                    except OSError:
                        pass
                return DownloadResult(
                    task.bvid, "success", local_path, content_type, size, attempt,
                    response.status_code, None,
                )
        except StopDownload:
            raise
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as error:
            last_error = f"{type(error).__name__}: {error}"
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass

        if not retryable or attempt == config.retries:
            return DownloadResult(
                task.bvid, "failed", None, None, None, attempt,
                last_http_status, last_error,
            )
        delay = max(retry_after, config.retry_delay * 2 ** (attempt - 1))
        if stopped.wait(delay + random.uniform(0, min(delay * 0.2, 1.0))):
            raise StopDownload

    raise AssertionError("unreachable")


def save_result(connection: sqlite3.Connection, result: DownloadResult) -> None:
    connection.execute(
        """
        UPDATE covers
        SET status=?, local_path=COALESCE(?, local_path), content_type=?, size_bytes=?,
            attempts=attempts+?, http_status=?, error=?, updated_at=?
        WHERE bvid=?
        """,
        (
            result.status,
            result.local_path,
            result.content_type,
            result.size_bytes,
            result.attempts,
            result.http_status,
            result.error,
            now(),
            result.bvid,
        ),
    )


def run_downloads(connection: sqlite3.Connection, config: Config) -> dict[str, int]:
    stopped = threading.Event()
    cooldown = RiskCooldown()
    counts = {"success": 0, "failed": 0}
    limits = httpx.Limits(
        max_connections=config.workers,
        max_keepalive_connections=config.workers,
    )
    client = httpx.Client(headers=HEADERS, timeout=config.timeout, limits=limits, follow_redirects=True)
    executor = ThreadPoolExecutor(max_workers=config.workers, thread_name_prefix="bili-cover")
    futures: dict[Future[DownloadResult], CoverTask] = {}
    tasks = pending_tasks(connection, config)
    exhausted = False
    processed = 0
    try:
        while futures or not exhausted:
            while not exhausted and len(futures) < config.workers * 2:
                try:
                    task = next(tasks)
                except StopIteration:
                    exhausted = True
                    break
                future = executor.submit(download_cover, task, config, client, cooldown, stopped)
                futures[future] = task
            if not futures:
                break
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                task = futures.pop(future)
                try:
                    result = future.result()
                except StopDownload:
                    continue
                except Exception as error:  # 保留单个任务异常，不中断整个批次
                    result = DownloadResult(
                        task.bvid, "failed", None, None, None, 1, None,
                        f"{type(error).__name__}: {error}",
                    )
                save_result(connection, result)
                counts[result.status] += 1
                processed += 1
                if result.status == "failed":
                    LOG.warning("%s 下载失败: %s", result.bvid, result.error)
                if processed % 100 == 0:
                    connection.commit()
                    LOG.info(
                        "本次已处理 %s，成功 %s，失败 %s",
                        f"{processed:,}", f"{counts['success']:,}", f"{counts['failed']:,}",
                    )
        connection.commit()
    except KeyboardInterrupt:
        LOG.warning("收到中断，正在保存状态并停止……")
        stopped.set()
        for future in futures:
            future.cancel()
        connection.commit()
    finally:
        stopped.set()
        executor.shutdown(wait=True, cancel_futures=True)
        client.close()
    return counts


def database_status(connection: sqlite3.Connection) -> dict[str, int]:
    rows = connection.execute(
        "SELECT status, COUNT(*) FROM covers GROUP BY status ORDER BY status"
    ).fetchall()
    return {str(status): int(count) for status, count in rows}


def main() -> int:
    config = parse_args()
    connection: sqlite3.Connection | None = None
    try:
        files = discover_creator_files(config.input_dir)
        connection = connect_database(config.database)
        scan = scan_results(connection, files)
        LOG.info(
            "扫描完成：作者文件 %s，作品记录 %s，唯一封面 %s，无效记录 %s",
            f"{scan['creator_files']:,}", f"{scan['scanned_videos']:,}",
            f"{scan['unique_bvids']:,}", f"{scan['invalid_records']:,}",
        )
        if not config.scan_only:
            result = run_downloads(connection, config)
            LOG.info("下载结束：成功 %s，失败 %s", f"{result['success']:,}", f"{result['failed']:,}")
        LOG.info("数据库状态：%s", database_status(connection))
        LOG.info("封面目录：%s", config.covers_dir)
        LOG.info("状态数据库：%s", config.database)
        return 2 if scan["invalid_files"] else 0
    except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as error:
        LOG.error("执行失败: %s", error)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
