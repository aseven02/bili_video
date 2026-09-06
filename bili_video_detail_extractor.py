# -*- coding: utf-8 -*-
"""
按作者分组批量提取 Bilibili 视频详情与播放候选地址。

输入 JSON 支持：
[
    {"uid": "434377496", "videos": ["BV...", {"bvid": "BV..."}]}
]

脚本复用 bili_creator_demo.py 的持久浏览器登录态、Cookie、UA、WBI 签名、
风控刷新和原子写盘能力。视频文件不会被下载，只保存 durl/DASH 候选地址。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import random
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tqdm import tqdm
import httpx
from playwright.async_api import async_playwright

from bili_creator_demo import (
    BILI_API,
    BILI_HOME,
    DEFAULT_USER_AGENT,
    BrowserSession,
    RiskControlError,
    atomic_write_json,
    build_browser_session,
    close_browser_context_safely,
    refresh_browser_session,
    utc_now_text,
)


DEFAULT_OUTPUT = pathlib.Path("bili_video_details.json")
DEFAULT_USER_DATA_DIR = pathlib.Path("chrome_data")


@dataclass(frozen=True)
class DetailConfig:
    """一次详情抓取任务的运行配置。"""

    input_path: pathlib.Path
    output_path: pathlib.Path
    user_data_dir: pathlib.Path
    cookie: str
    headless: bool
    qn: int
    retries: int
    retry_delay: float
    risk_control_delay: float
    request_timeout: float
    video_sleep_min: float
    video_sleep_max: float
    resume: bool
    refresh_success: bool


@dataclass(frozen=True)
class VideoRef:
    """视频接口支持 bvid 或 aid 二选一。"""

    source: str
    bvid: str | None = None
    aid: int | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按作者分组提取 Bilibili 视频详情和播放地址")
    parser.add_argument(
        "--input",
        "-i",
        required=True,
        help="输入 JSON；格式为 [{\"uid\": \"...\", \"videos\": [...]}, ...]",
    )
    parser.add_argument("--output", "-o", default=str(DEFAULT_OUTPUT), help="输出及断点 JSON 文件")
    parser.add_argument("--cookie", default="", help="可选 Cookie；也可在打开的浏览器中登录")
    parser.add_argument("--user-data-dir", default=str(DEFAULT_USER_DATA_DIR), help="持久浏览器资料目录")
    parser.add_argument("--headless", action="store_true", help="无界面运行；需已有有效登录态")
    parser.add_argument("--qn", type=int, default=80, help="请求清晰度，默认 80（1080P）")
    parser.add_argument("--retries", type=int, default=3, help="每个 API 请求的最大尝试次数")
    parser.add_argument("--retry-delay", type=float, default=2.0, help="普通错误首次重试等待秒数")
    parser.add_argument("--risk-control-delay", type=float, default=300.0, help="命中 -352 风控后的等待秒数")
    parser.add_argument("--request-timeout", type=float, default=30.0, help="单次 HTTP 请求超时秒数")
    parser.add_argument("--video-sleep-min", type=float, default=0.8, help="视频之间最短随机等待秒数")
    parser.add_argument("--video-sleep-max", type=float, default=2.0, help="视频之间最长随机等待秒数")
    parser.add_argument("--no-resume", action="store_true", help="忽略已有输出，从头创建结果")
    parser.add_argument(
        "--refresh-success",
        action="store_true",
        help="重新抓取已成功视频；适合刷新可能过期的播放地址",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> DetailConfig:
    if args.qn <= 0:
        raise ValueError("--qn must be greater than 0")
    if args.retries < 1:
        raise ValueError("--retries must be greater than 0")
    if args.retry_delay < 0 or args.risk_control_delay < 0:
        raise ValueError("retry delays must not be negative")
    if args.request_timeout <= 0:
        raise ValueError("--request-timeout must be greater than 0")
    if args.video_sleep_min < 0 or args.video_sleep_max < 0:
        raise ValueError("video sleep values must not be negative")
    if args.video_sleep_min > args.video_sleep_max:
        raise ValueError("--video-sleep-min must be less than or equal to --video-sleep-max")

    return DetailConfig(
        input_path=pathlib.Path(args.input).expanduser(),
        output_path=pathlib.Path(args.output).expanduser(),
        user_data_dir=pathlib.Path(args.user_data_dir).expanduser(),
        cookie=args.cookie.strip(),
        headless=args.headless,
        qn=args.qn,
        retries=args.retries,
        retry_delay=args.retry_delay,
        risk_control_delay=args.risk_control_delay,
        request_timeout=args.request_timeout,
        video_sleep_min=args.video_sleep_min,
        video_sleep_max=args.video_sleep_max,
        resume=not args.no_resume,
        refresh_success=args.refresh_success,
    )


def parse_video_ref(raw: str) -> VideoRef:
    """从 URL、BV 号、av 号或纯 aid 中解析视频标识。"""
    value = raw.strip()
    if not value:
        raise ValueError("视频标识不能为空")

    bvid_match = re.search(r"(BV[a-zA-Z0-9]+)", value)
    if bvid_match:
        return VideoRef(source=value, bvid=bvid_match.group(1))

    av_match = re.search(r"(?:^|/)[aA][vV](\d+)(?:[/?#]|$)", value)
    if av_match:
        return VideoRef(source=value, aid=int(av_match.group(1)))

    if value.isdigit():
        return VideoRef(source=value, aid=int(value))
    raise ValueError(f"无法解析视频 URL/BV/av: {raw}")


def video_source(raw_video: Any) -> str:
    """兼容字符串及作品列表脚本产生的视频对象。"""
    if isinstance(raw_video, (str, int)):
        return str(raw_video).strip()
    if not isinstance(raw_video, dict):
        raise ValueError(f"不支持的视频项类型: {type(raw_video).__name__}")

    # 输入对象可直接带 url/bvid/aid，也兼容旧详情结果中的嵌套 video。
    nested = raw_video.get("video") if isinstance(raw_video.get("video"), dict) else {}
    for container in (raw_video, nested):
        for key in ("url", "source_url", "resolved_url", "bvid"):
            if container.get(key):
                return str(container[key]).strip()
        if container.get("aid") is not None:
            return f"av{container['aid']}"
    raise ValueError(f"视频项缺少 url、bvid 或 aid: {raw_video}")


def input_video_key(source: str) -> str:
    """在尚未请求详情时，为输入视频生成稳定去重键。"""
    try:
        ref = parse_video_ref(source)
    except ValueError:
        return source.strip().lower()
    return ref.bvid or f"av{ref.aid}"


def load_creator_tasks(path: pathlib.Path) -> list[dict[str, Any]]:
    """读取作者分组 JSON，根节点兼容数组或 {\"creators\": [...]}。"""
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    raw_creators = payload.get("creators") if isinstance(payload, dict) else payload
    if not isinstance(raw_creators, list):
        raise ValueError("输入 JSON 必须是作者数组，或包含 creators 数组")

    creators: list[dict[str, Any]] = []
    seen_uids: set[str] = set()
    for index, raw_creator in enumerate(raw_creators):
        if not isinstance(raw_creator, dict):
            raise ValueError(f"第 {index + 1} 个作者项必须是对象")
        uid = str(raw_creator.get("uid") or raw_creator.get("mid") or "").strip()
        if not uid or not uid.isdigit():
            raise ValueError(f"第 {index + 1} 个作者缺少有效数字 uid")
        if uid in seen_uids:
            raise ValueError(f"输入中存在重复 uid: {uid}")
        seen_uids.add(uid)

        raw_videos = raw_creator.get("videos")
        if not isinstance(raw_videos, list):
            raise ValueError(f"UID {uid} 的 videos 必须是数组")

        videos: list[dict[str, Any]] = []
        seen_videos: set[str] = set()
        for raw_video in raw_videos:
            source = video_source(raw_video)
            key = input_video_key(source)
            if key in seen_videos:
                continue
            seen_videos.add(key)
            videos.append({"source": source, "status": "pending", "error": None})
        creators.append({"uid": uid, "videos": videos})
    return creators


def new_output_document(config: DetailConfig) -> dict[str, Any]:
    return {
        "generated_at": utc_now_text(),
        "updated_at": utc_now_text(),
        "config": {
            "source_input": str(config.input_path),
            "qn": config.qn,
            "retries": config.retries,
            "retry_delay": config.retry_delay,
            "risk_control_delay": config.risk_control_delay,
            "request_timeout": config.request_timeout,
        },
        "creators": [],
    }


def output_video_key(video: dict[str, Any]) -> str:
    return str(video.get("bvid") or (f"av{video['aid']}" if video.get("aid") else input_video_key(video["source"])))


def merge_input_into_output(
    document: dict[str, Any],
    input_creators: list[dict[str, Any]],
    refresh_success: bool,
) -> None:
    """把新输入并入断点文件，保留已经完成的作者和作品数据。"""
    existing_creators = {str(item.get("uid")): item for item in document["creators"]}
    ordered: list[dict[str, Any]] = []

    for input_creator in input_creators:
        uid = input_creator["uid"]
        creator = existing_creators.get(uid)
        if creator is None:
            creator = {
                "uid": uid,
                "author": None,
                "author_fetched_at": None,
                "status": "pending",
                "started_at": None,
                "finished_at": None,
                "error": None,
                "videos": [],
            }
        creator.setdefault("author", None)
        creator.setdefault("videos", [])

        old_videos: dict[str, dict[str, Any]] = {}
        for item in creator["videos"]:
            # 成功结果通常已有 bvid，但短链输入仍需用原始 source 才能在续跑时命中。
            old_videos[output_video_key(item)] = item
            if item.get("source"):
                old_videos[input_video_key(str(item["source"]))] = item
        merged_videos: list[dict[str, Any]] = []
        for input_video in input_creator["videos"]:
            key = input_video_key(input_video["source"])
            video = old_videos.get(key, input_video)
            video.setdefault("source", input_video["source"])
            video.setdefault("status", "pending")
            video.setdefault("error", None)
            if refresh_success and video.get("status") == "success":
                video["status"] = "pending"
                video["error"] = None
            merged_videos.append(video)
        creator["videos"] = merged_videos
        ordered.append(creator)

    # 输出只保留本次输入中的作者，防止旧任务意外混入本次结果。
    document["creators"] = ordered


def load_or_create_output(config: DetailConfig, creators: list[dict[str, Any]]) -> dict[str, Any]:
    if config.resume and config.output_path.exists():
        with config.output_path.open("r", encoding="utf-8") as file:
            document = json.load(file)
        if not isinstance(document, dict) or not isinstance(document.get("creators"), list):
            raise ValueError(f"无法识别已有输出格式: {config.output_path}")
    else:
        document = new_output_document(config)

    merge_input_into_output(document, creators, config.refresh_success)
    return document


def local_time_text(timestamp: Any) -> str | None:
    if not timestamp:
        return None
    return datetime.fromtimestamp(int(timestamp)).astimezone().isoformat(timespec="seconds")


def compact_official(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {
        "type": raw.get("type") if raw.get("type") is not None else raw.get("role"),
        "title": raw.get("title"),
        "description": raw.get("desc") or raw.get("description"),
    }


def compact_author(uid: str, data: dict[str, Any]) -> dict[str, Any]:
    """作者资料来自一次 /x/web-interface/card 请求。"""
    card = data.get("card") or {}
    level_info = card.get("level_info") or {}
    official = card.get("Official") or card.get("official") or card.get("official_verify")
    return {
        "uid": str(card.get("mid") or uid),
        "name": card.get("name"),
        "face": card.get("face"),
        "fans": data.get("follower") if data.get("follower") is not None else card.get("fans"),
        "like_num": data.get("like_num"),
        "level": level_info.get("current_level"),
        "sign": card.get("sign"),
        "description": card.get("description") or card.get("sign"),
        "official": compact_official(official),
        "archive_count": data.get("archive_count"),
        "article_count": data.get("article_count"),
        "space_url": f"https://space.bilibili.com/{uid}",
    }


def compact_staff(view: dict[str, Any]) -> list[dict[str, Any]]:
    """只保存合作作者，排除 staff 中可能重复出现的主投稿人。"""
    owner_uid = str((view.get("owner") or {}).get("mid") or "")
    collaborators = []
    for item in view.get("staff") or []:
        if str(item.get("mid") or "") == owner_uid:
            continue
        collaborators.append(
            {
                "uid": str(item.get("mid")) if item.get("mid") is not None else None,
                "name": item.get("name"),
                "face": item.get("face"),
                "role": item.get("title"),
                "official": compact_official(item.get("official")),
            }
        )
    return collaborators


def compact_durl(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": data.get("format"),
        "quality": data.get("quality"),
        "accept_quality": data.get("accept_quality") or [],
        "accept_description": data.get("accept_description") or [],
        "items": [
            {
                "order": item.get("order"),
                "url": item.get("url"),
                "backup_urls": item.get("backup_url") or [],
                "size": item.get("size"),
                "length_ms": item.get("length"),
                "mime_type": item.get("mime_type"),
            }
            for item in data.get("durl") or []
        ],
    }


def compact_dash_item(item: dict[str, Any]) -> dict[str, Any]:
    segment_base = item.get("SegmentBase") or item.get("segment_base") or {}
    return {
        "id": item.get("id"),
        "base_url": item.get("baseUrl") or item.get("base_url"),
        "backup_urls": item.get("backupUrl") or item.get("backup_url") or [],
        "bandwidth": item.get("bandwidth"),
        "mime_type": item.get("mimeType") or item.get("mime_type"),
        "codecs": item.get("codecs"),
        "width": item.get("width"),
        "height": item.get("height"),
        "frame_rate": item.get("frameRate") or item.get("frame_rate"),
        "codecid": item.get("codecid"),
        "segment_base": {
            "initialization": segment_base.get("Initialization") or segment_base.get("initialization"),
            "index_range": segment_base.get("indexRange") or segment_base.get("index_range"),
        },
    }


def compact_dash(data: dict[str, Any]) -> dict[str, Any]:
    dash = data.get("dash") or {}
    return {
        "quality": data.get("quality"),
        "accept_quality": data.get("accept_quality") or [],
        "accept_description": data.get("accept_description") or [],
        "duration": dash.get("duration"),
        "min_buffer_time": dash.get("minBufferTime") or dash.get("min_buffer_time"),
        "video": [compact_dash_item(item) for item in dash.get("video") or []],
        "audio": [compact_dash_item(item) for item in dash.get("audio") or []],
    }


def compact_video(
    source: str,
    view: dict[str, Any],
    durl_data: dict[str, Any],
    dash_data: dict[str, Any],
) -> dict[str, Any]:
    stat = view.get("stat") or {}
    rights = view.get("rights") or {}
    collaborators = compact_staff(view)
    bvid = view.get("bvid") or ""
    return {
        "source": source,
        "status": "success",
        "fetched_at": utc_now_text(),
        "error": None,
        "aid": view.get("aid"),
        "bvid": bvid,
        "cid": view.get("cid"),
        "title": view.get("title"),
        "description": view.get("desc"),
        "pubdate": view.get("pubdate"),
        "pubdate_text": local_time_text(view.get("pubdate")),
        "cover": view.get("pic"),
        "duration": view.get("duration"),
        "owner_uid": str((view.get("owner") or {}).get("mid") or ""),
        "is_union_video": bool(rights.get("is_cooperation") or collaborators),
        "cooperation_authors": collaborators,
        "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
        "stats": {
            "view": stat.get("view"),
            "like": stat.get("like"),
            "coin": stat.get("coin"),
            "favorite": stat.get("favorite"),
            "share": stat.get("share"),
            "danmaku": stat.get("danmaku"),
            "reply": stat.get("reply"),
        },
        "pages": [
            {
                "cid": item.get("cid"),
                "page": item.get("page"),
                "part": item.get("part"),
                "duration": item.get("duration"),
                "width": (item.get("dimension") or {}).get("width"),
                "height": (item.get("dimension") or {}).get("height"),
            }
            for item in view.get("pages") or []
        ],
        "play_urls": {
            "note": "播放地址通常有时效性；下载请求需携带 Referer 和当前登录 Cookie。",
            "durl": compact_durl(durl_data),
            "dash": compact_dash(dash_data),
        },
    }


class BilibiliDetailCrawler:
    """复用一个浏览器登录会话，顺序抓取作者与作品详情。"""

    def __init__(
        self,
        context: Any,
        client: httpx.AsyncClient,
        session: BrowserSession,
        config: DetailConfig,
    ) -> None:
        self.context = context
        self.client = client
        self.session = session
        self.config = config

    def request_headers(self, referer: str) -> dict[str, str]:
        return {
            "User-Agent": self.session.user_agent,
            "Referer": referer,
            "Origin": BILI_HOME,
            "Cookie": self.session.cookie_header,
        }

    async def request_api(
        self,
        path: str,
        params: dict[str, Any],
        *,
        referer: str,
        need_sign: bool = False,
        label: str,
    ) -> dict[str, Any]:
        """统一处理 HTTP、业务错误、风控冷却、指数退避和会话刷新。"""
        last_error: Exception | None = None
        for attempt in range(1, self.config.retries + 1):
            try:
                request_params = dict(params)
                if need_sign:
                    request_params = self.session.signer.sign(request_params)
                response = await self.client.get(
                    f"{BILI_API}{path}",
                    params=request_params,
                    headers=self.request_headers(referer),
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("code") == -352:
                    raise RiskControlError(payload)
                if payload.get("code") != 0:
                    raise RuntimeError(
                        f"API code={payload.get('code')}, message={payload.get('message')}"
                    )
                return payload.get("data") or {}
            except RiskControlError as error:
                last_error = error
                if attempt >= self.config.retries:
                    break
                print(
                    f"{label} hit risk control ({attempt}/{self.config.retries}). "
                    f"Cool down {self.config.risk_control_delay:.1f}s..."
                )
                await asyncio.sleep(self.config.risk_control_delay)
                self.session = await refresh_browser_session(self.context, self.session, self.client)
            except (httpx.HTTPError, RuntimeError, ValueError) as error:
                last_error = error
                if attempt >= self.config.retries:
                    break
                delay = self.config.retry_delay * (2 ** (attempt - 1))
                print(
                    f"{label} failed ({attempt}/{self.config.retries}): {error}. "
                    f"Retry after {delay:.1f}s..."
                )
                await asyncio.sleep(delay)
                self.session = await refresh_browser_session(self.context, self.session, self.client)

        raise RuntimeError(f"{label} failed after {self.config.retries} attempts: {last_error}")

    async def resolve_short_url(self, source: str) -> str:
        if "b23.tv" not in source:
            return source
        response = await self.client.get(
            source,
            headers=self.request_headers(BILI_HOME),
            follow_redirects=True,
        )
        response.raise_for_status()
        return str(response.url)

    async def fetch_author(self, uid: str) -> dict[str, Any]:
        # 关键步骤：每个 UID 只调用一次作者卡片接口，避免随每个视频重复查作者。
        data = await self.request_api(
            "/x/web-interface/card",
            {"mid": uid, "photo": "true"},
            referer=f"https://space.bilibili.com/{uid}",
            label=f"UID {uid} author",
        )
        return compact_author(uid, data)

    async def fetch_view(self, ref: VideoRef) -> dict[str, Any]:
        params: dict[str, Any] = {"bvid": ref.bvid} if ref.bvid else {"aid": ref.aid}
        # /view 只取作品详情；作者的完整资料由上面的 card 接口按 UID 单独获取。
        return await self.request_api(
            "/x/web-interface/view",
            params,
            referer=ref.source if ref.source.startswith("http") else BILI_HOME,
            label=f"video {ref.bvid or ref.aid} detail",
        )

    async def fetch_play_url(self, aid: int, cid: int, fnval: int, label: str) -> dict[str, Any]:
        return await self.request_api(
            "/x/player/wbi/playurl",
            {
                "avid": aid,
                "cid": cid,
                "qn": self.config.qn,
                "fourk": 1,
                "fnval": fnval,
                "platform": "pc",
            },
            referer=f"https://www.bilibili.com/video/av{aid}",
            need_sign=True,
            label=label,
        )

    async def fetch_video(self, source: str) -> dict[str, Any]:
        resolved = await self.resolve_short_url(source)
        ref = parse_video_ref(resolved)
        view = await self.fetch_view(ref)
        aid = int(view.get("aid") or 0)
        cid = int(view.get("cid") or 0)
        if not aid or not cid:
            raise RuntimeError("作品详情未返回有效 aid/cid")

        # 关键步骤：分别请求传统 durl 与 DASH；两种协议通常不会同时出现在一次响应中。
        durl_data = await self.fetch_play_url(aid, cid, 1, f"video {view.get('bvid')} durl")
        dash_data = await self.fetch_play_url(aid, cid, 4048, f"video {view.get('bvid')} DASH")
        return compact_video(source, view, durl_data, dash_data)


def pending_videos(creator: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in creator["videos"] if item.get("status") != "success"]


def update_creator_summary(creator: dict[str, Any]) -> None:
    success_count = sum(item.get("status") == "success" for item in creator["videos"])
    failed_count = sum(item.get("status") == "failed" for item in creator["videos"])
    creator["video_count"] = len(creator["videos"])
    creator["success_count"] = success_count
    creator["failed_count"] = failed_count


async def crawl_creator(
    crawler: BilibiliDetailCrawler,
    config: DetailConfig,
    document: dict[str, Any],
    creator: dict[str, Any],
) -> None:
    uid = creator["uid"]
    todo = pending_videos(creator)
    if config.resume and creator.get("author") and not todo:
        print(f"UID {uid} already finished. Skip.")
        return

    creator["status"] = "running"
    creator["started_at"] = creator.get("started_at") or utc_now_text()
    creator["finished_at"] = None
    creator["error"] = None
    atomic_write_json(config.output_path, document)

    try:
        if not creator.get("author"):
            creator["author"] = await crawler.fetch_author(uid)
            creator["author_fetched_at"] = utc_now_text()
            atomic_write_json(config.output_path, document)

        for index, video in enumerate(todo, start=1):
            source = video["source"]
            print(f"UID {uid} video {index}/{len(todo)}: {source}")
            video["status"] = "running"
            video["error"] = None
            video["last_attempt_at"] = utc_now_text()
            atomic_write_json(config.output_path, document)

            try:
                result = await crawler.fetch_video(source)
                video.clear()
                video.update(result)
                print(f"  saved {video.get('bvid')} | {video.get('title')}")
            except Exception as error:
                # 单个视频失败后立即记入断点，并继续同作者的其他视频。
                video["status"] = "failed"
                video["error"] = str(error)
                video["failed_at"] = utc_now_text()
                print(f"  failed: {error}")

            update_creator_summary(creator)
            atomic_write_json(config.output_path, document)
            if index < len(todo):
                delay = random.uniform(config.video_sleep_min, config.video_sleep_max)
                if delay:
                    await asyncio.sleep(delay)

        update_creator_summary(creator)
        if creator["failed_count"]:
            creator["status"] = "partial_failed"
            creator["error"] = f"{creator['failed_count']} video(s) failed; rerun to retry them"
        else:
            creator["status"] = "success"
            creator["error"] = None
    except Exception as error:
        # 作者信息抓取失败时保留整个作者任务，续跑会再次尝试该作者 API。
        creator["status"] = "failed"
        creator["error"] = str(error)
        print(f"UID {uid} failed: {error}")
    finally:
        creator["finished_at"] = utc_now_text()
        update_creator_summary(creator)
        atomic_write_json(config.output_path, document)


async def crawl(config: DetailConfig) -> None:
    input_creators = load_creator_tasks(config.input_path)
    document = load_or_create_output(config, input_creators)
    config.user_data_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(config.output_path, document)

    timeout = httpx.Timeout(config.request_timeout)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(config.user_data_dir),
                headless=config.headless,
                viewport={"width": 1280, "height": 900},
                user_agent=DEFAULT_USER_AGENT,
            )
            try:
                page = await context.new_page()
                session = await build_browser_session(context, page, client, config.cookie)
                crawler = BilibiliDetailCrawler(context, client, session, config)
                for index, creator in enumerate(document["creators"], start=1):
                    print(f"\nStart UID {creator['uid']} ({index}/{len(document['creators'])})")
                    await crawl_creator(crawler, config, document, creator)
            finally:
                await close_browser_context_safely(context)


def main() -> None:
    config = build_config(parse_args())
    try:
        asyncio.run(crawl(config))
    except KeyboardInterrupt:
        print("\n已中断。作者资料及每个已完成视频均已写入输出，下次使用同一命令即可续跑。")


if __name__ == "__main__":
    main()
