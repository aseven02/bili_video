# -*- coding: utf-8 -*-
"""
Batch crawl Bilibili creator videos by UID.

Examples:
    uv run python bili_creator_demo.py --creator 434377496
    uv run python bili_creator_demo.py --creators 434377496,23972272 --max-pages 2
    uv run python bili_creator_demo.py --uid-json uids.json --output output/videos.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import random
import re
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from http.cookies import SimpleCookie
from typing import Any

import httpx
from playwright.async_api import BrowserContext, Error as PlaywrightError, Page, async_playwright

from bili_signin import BilibiliSign


BILI_HOME = "https://www.bilibili.com"
BILI_API = "https://api.bilibili.com"
DEFAULT_OUTPUT = pathlib.Path("bili_creator_videos.json")
DEFAULT_USER_DATA_DIR = pathlib.Path("chrome_data")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class CrawlConfig:
    """保存一次抓取任务的完整配置，避免在流程中传递零散参数。"""

    creator_ids: list[str]
    max_pages: int
    page_size: int
    sleep_seconds: float
    page_sleep_min: float
    page_sleep_max: float
    creator_sleep_min: float
    creator_sleep_max: float
    headless: bool
    cookie: str
    user_data_dir: pathlib.Path
    output: pathlib.Path
    retries: int
    retry_delay: float
    risk_control_delay: float
    request_timeout: float
    resume: bool


@dataclass(frozen=True)
class BrowserSession:
    """保存浏览器态衍生出的请求信息，包括 Cookie、UA 和 WBI 签名器。"""

    page: Page
    user_agent: str
    cookie_header: str
    signer: BilibiliSign


class RiskControlError(RuntimeError):
    """表示 B 站返回了风控校验失败，需要按长冷却处理。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        self.v_voucher = data.get("v_voucher")
        super().__init__(f"creator videos api risk control failed: {payload}")


def parse_args() -> argparse.Namespace:
    """解析命令行参数，保留原始字符串形式供后续校验和归一化。"""
    parser = argparse.ArgumentParser(description="Batch Bilibili creator video crawler demo")
    parser.add_argument(
        "--creator",
        action="append",
        default=[],
        help="Creator UID or space URL. Can be passed more than once.",
    )
    parser.add_argument(
        "--creators",
        default="",
        help="Comma-separated creator UIDs or space URLs, e.g. 434377496,23972272",
    )
    parser.add_argument(
        "--uid-json",
        default="",
        help="JSON file path. Supports ['uid'] or {'uids': ['uid']}.",
    )
    parser.add_argument("--max-pages", type=int, default=10, help="Maximum video pages to crawl for each UID")
    parser.add_argument("--page-size", type=int, default=40, help="Page size for creator videos")
    parser.add_argument(
        "--sleep",
        type=float,
        default=None,
        help="Fixed sleep seconds between pages. Overrides --sleep-min/--sleep-max.",
    )
    parser.add_argument("--sleep-min", type=float, default=0.5, help="Minimum random sleep seconds between pages")
    parser.add_argument("--sleep-max", type=float, default=2.0, help="Maximum random sleep seconds between pages")
    parser.add_argument(
        "--creator-sleep-min",
        type=float,
        default=3.0,
        help="Minimum random sleep seconds between creators",
    )
    parser.add_argument(
        "--creator-sleep-max",
        type=float,
        default=10.0,
        help="Maximum random sleep seconds between creators",
    )
    parser.add_argument("--retries", type=int, default=2, help="Retry times for API requests")
    parser.add_argument("--retry-delay", type=float, default=20.0, help="Initial retry delay in seconds")
    parser.add_argument(
        "--risk-control-delay",
        type=float,
        default=300.0,
        help="Sleep seconds before retrying after Bilibili risk-control code -352",
    )
    parser.add_argument("--request-timeout", type=float, default=30.0, help="HTTP request timeout in seconds")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--no-resume", action="store_true", help="Ignore existing output and crawl from scratch")
    parser.add_argument("--cookie", default="", help="Optional raw cookie string, e.g. 'SESSDATA=...; bili_jct=...'")
    parser.add_argument(
        "--user-data-dir",
        default=str(DEFAULT_USER_DATA_DIR),
        help="Playwright persistent browser profile path",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="JSON output path",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> CrawlConfig:
    """校验命令行参数并转换为抓取运行所需的配置对象。"""
    # 先校验会直接影响请求规模和稳定性的参数，避免启动浏览器后才失败。
    if args.max_pages < 1:
        raise ValueError("--max-pages must be greater than 0")
    if not 1 <= args.page_size <= 50:
        raise ValueError("--page-size must be between 1 and 50")
    if args.sleep is not None and args.sleep < 0:
        raise ValueError("--sleep must not be negative")
    if args.sleep_min < 0 or args.sleep_max < 0:
        raise ValueError("--sleep-min/--sleep-max must not be negative")
    if args.sleep_min > args.sleep_max:
        raise ValueError("--sleep-min must be less than or equal to --sleep-max")
    if args.creator_sleep_min < 0 or args.creator_sleep_max < 0:
        raise ValueError("--creator-sleep-min/--creator-sleep-max must not be negative")
    if args.creator_sleep_min > args.creator_sleep_max:
        raise ValueError("--creator-sleep-min must be less than or equal to --creator-sleep-max")
    if args.retries < 1:
        raise ValueError("--retries must be greater than 0")
    if args.retry_delay < 0:
        raise ValueError("--retry-delay must not be negative")
    if args.risk_control_delay < 0:
        raise ValueError("--risk-control-delay must not be negative")
    if args.request_timeout <= 0:
        raise ValueError("--request-timeout must be greater than 0")

    # 兼容旧参数：传了 --sleep 就使用固定页面间隔，否则使用默认随机区间。
    page_sleep_min = args.sleep if args.sleep is not None else args.sleep_min
    page_sleep_max = args.sleep if args.sleep is not None else args.sleep_max

    # UID 可能来自多个入口，这里统一解析、去重并保持用户输入顺序。
    creator_ids = collect_creator_ids(args.creator, args.creators, args.uid_json)
    if not creator_ids:
        raise ValueError("provide at least one UID with --creator, --creators, or --uid-json")

    return CrawlConfig(
        creator_ids=creator_ids,
        max_pages=args.max_pages,
        page_size=args.page_size,
        sleep_seconds=page_sleep_min,
        page_sleep_min=page_sleep_min,
        page_sleep_max=page_sleep_max,
        creator_sleep_min=args.creator_sleep_min,
        creator_sleep_max=args.creator_sleep_max,
        headless=args.headless,
        cookie=args.cookie.strip(),
        user_data_dir=pathlib.Path(args.user_data_dir).expanduser(),
        output=pathlib.Path(args.output).expanduser(),
        retries=args.retries,
        retry_delay=args.retry_delay,
        risk_control_delay=args.risk_control_delay,
        request_timeout=args.request_timeout,
        resume=not args.no_resume,
    )


def collect_creator_ids(raw_creators: list[str], comma_creators: str, uid_json: str) -> list[str]:
    """汇总不同参数来源里的 UP 主 UID，并按首次出现顺序去重。"""
    values: list[str] = []
    # 支持多次 --creator、逗号分隔参数，以及 JSON 文件三种输入方式。
    values.extend(raw_creators)
    values.extend(item for item in comma_creators.split(",") if item.strip())
    values.extend(load_uid_json(pathlib.Path(uid_json).expanduser()) if uid_json else [])

    creator_ids: list[str] = []
    seen: set[str] = set()
    for value in values:
        # 每个值都走同一个解析函数，让 UID 和空间 URL 的处理规则一致。
        creator_id = parse_creator_id(str(value))
        if creator_id not in seen:
            creator_ids.append(creator_id)
            seen.add(creator_id)
    return creator_ids


def load_uid_json(path: pathlib.Path) -> list[str]:
    """从 JSON 文件中读取 UID 列表，兼容列表和常见字典字段格式。"""
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    # 直接的数组格式：["434377496", "23972272"]。
    if isinstance(payload, list):
        return [str(item) for item in payload]
    if isinstance(payload, dict):
        # 字典格式允许不同命名，便于和其他脚本产物对接。
        for key in ("uids", "creator_ids", "creators"):
            value = payload.get(key)
            if isinstance(value, list):
                return [str(item) for item in value]

    raise ValueError(f"unsupported uid json format: {path}")


def parse_creator_id(value: str) -> str:
    """把纯 UID 或 B 站空间 URL 解析成数字 UID 字符串。"""
    creator = value.strip()
    if not creator:
        raise ValueError("creator is required")
    # 已经是纯数字时直接返回，避免不必要的 URL 解析。
    if creator.isdigit():
        return creator

    parsed = urllib.parse.urlparse(creator)
    # 只接受 B 站域名，避免误把其他 URL 路径里的数字当 UID。
    if parsed.netloc and "bilibili.com" not in parsed.netloc:
        raise ValueError(f"unsupported creator url host: {parsed.netloc}")

    # 常见格式：https://www.bilibili.com/space/434377496
    match = re.search(r"/space/(\d+)", parsed.path)
    if match:
        return match.group(1)

    # 常见格式：https://space.bilibili.com/434377496
    if parsed.netloc.startswith("space.bilibili.com"):
        match = re.search(r"^/(\d+)", parsed.path)
        if match:
            return match.group(1)

    raise ValueError(f"cannot parse creator id from: {value}")


def utc_now_text() -> str:
    """生成不带微秒的 UTC ISO 时间字符串，用于输出文件时间戳。"""
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def random_sleep_seconds(min_seconds: float, max_seconds: float) -> float:
    """在给定区间内生成随机等待秒数，区间相等时返回固定值。"""
    if min_seconds == max_seconds:
        return min_seconds
    return random.uniform(min_seconds, max_seconds)


async def sleep_with_log(reason: str, min_seconds: float, max_seconds: float) -> None:
    """打印等待原因并执行随机 sleep，让抓取节奏不要过于机械。"""
    delay = random_sleep_seconds(min_seconds, max_seconds)
    print(f"{reason}. Sleep {delay:.1f}s...")
    await asyncio.sleep(delay)


async def close_browser_context_safely(context: BrowserContext) -> None:
    """关闭 Playwright 浏览器上下文，忽略手动中断后常见的已关闭错误。"""
    try:
        await context.close()
    except PlaywrightError as error:
        # Ctrl+C 可能已经让 Playwright driver 断开，此时关闭上下文只是在清理残局。
        print(f"Browser context already closed or disconnected: {error}")


def cookie_header_from_playwright(cookies: list[dict[str, Any]]) -> str:
    """把 Playwright cookie 列表转换成 HTTP Cookie 请求头。"""
    return "; ".join(f"{item['name']}={item['value']}" for item in cookies)


def cookie_dict_from_header(cookie_header: str) -> dict[str, str]:
    """把原始 Cookie 请求头解析成字典，便于检查登录态和注入浏览器。"""
    parsed = SimpleCookie()
    parsed.load(cookie_header)
    return {key: morsel.value for key, morsel in parsed.items()}


async def inject_cookie_if_needed(context: BrowserContext, cookie_header: str) -> None:
    """当用户传入 --cookie 时，将 Cookie 写入 Playwright 浏览器上下文。"""
    if not cookie_header:
        return

    # B 站主站和 API 都在 bilibili.com 域下，统一写到顶级域方便复用。
    cookies = [
        {
            "name": key,
            "value": value,
            "domain": ".bilibili.com",
            "path": "/",
        }
        for key, value in cookie_dict_from_header(cookie_header).items()
    ]
    if cookies:
        await context.add_cookies(cookies)


async def wait_for_login(context: BrowserContext, page: Page) -> str:
    """没有可用缓存登录态时，等待用户手动登录并返回新的 Cookie 请求头。"""
    for _ in range(180):
        # 每秒读取一次浏览器 Cookie，出现登录 Cookie 就继续后续 API 抓取。
        cookie_header = cookie_header_from_playwright(await context.cookies([BILI_HOME]))
        cookie_names = set(cookie_dict_from_header(cookie_header))
        if "SESSDATA" in cookie_names or "DedeUserID" in cookie_names:
            return cookie_header
        await page.wait_for_timeout(1000)

    raise RuntimeError("Login timed out. Please scan/login in the opened browser, or pass --cookie.")


async def get_nav_data(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    """调用 B 站导航接口，获取登录状态和 WBI 图片信息等全局数据。"""
    response = await client.get(f"{BILI_API}/x/web-interface/nav", headers=headers)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"nav api failed: {payload}")
    return payload.get("data", {})


async def get_wbi_keys(page: Page, client: httpx.AsyncClient, headers: dict[str, str]) -> tuple[str, str]:
    """优先从 localStorage 读取 WBI key，失败时回退到 nav 接口。"""
    # 优先读页面 localStorage，减少一次网络请求，也复用浏览器当前状态。
    local_storage = await page.evaluate("() => ({ ...window.localStorage })")
    wbi_img_urls = local_storage.get("wbi_img_urls", "")
    if not wbi_img_urls:
        img_url = local_storage.get("wbi_img_url")
        sub_url = local_storage.get("wbi_sub_url")
        if img_url and sub_url:
            wbi_img_urls = f"{img_url}-{sub_url}"

    if wbi_img_urls and "-" in wbi_img_urls:
        img_url, sub_url = wbi_img_urls.split("-", 1)
    else:
        # localStorage 不完整时，从 nav 接口兜底获取 wbi_img 配置。
        nav_data = await get_nav_data(client, headers)
        wbi_img = nav_data.get("wbi_img") or {}
        img_url = wbi_img.get("img_url", "")
        sub_url = wbi_img.get("sub_url", "")

    if not img_url or not sub_url:
        raise RuntimeError("failed to load WBI keys from browser localStorage or nav API")

    # WBI 签名只需要图片文件名中的 key，不需要完整 URL。
    img_key = img_url.rsplit("/", 1)[1].split(".", 1)[0]
    sub_key = sub_url.rsplit("/", 1)[1].split(".", 1)[0]
    return img_key, sub_key


async def build_browser_session(
    context: BrowserContext,
    page: Page,
    client: httpx.AsyncClient,
    cookie_header: str,
) -> BrowserSession:
    """初始化浏览器会话，确认登录态并创建后续 API 请求需要的 WBI 签名器。"""
    # 先把命令行 Cookie 注入浏览器，再打开首页触发站点初始化逻辑。
    await inject_cookie_if_needed(context, cookie_header)
    await page.goto(BILI_HOME, wait_until="domcontentloaded")

    # 后续 httpx 请求复用浏览器 UA 和 Cookie，降低和网页环境不一致的风险。
    user_agent = await page.evaluate("() => navigator.userAgent")
    cookies = cookie_header_from_playwright(await context.cookies([BILI_HOME]))
    headers = {
        "User-Agent": user_agent,
        "Referer": BILI_HOME,
        "Origin": BILI_HOME,
        "Cookie": cookies,
    }
    nav_data = await get_nav_data(client, headers)

    if not nav_data.get("isLogin"):
        # 没有有效登录态时停在浏览器里等待用户手动扫码或账号登录。
        print("Not logged in yet. Please login in the opened browser window...")
        cookies = await wait_for_login(context, page)
        headers["Cookie"] = cookies

    # WBI key 会参与空间视频接口签名，必须在正式抓取前准备好。
    img_key, sub_key = await get_wbi_keys(page, client, headers)
    return BrowserSession(
        page=page,
        user_agent=user_agent,
        cookie_header=cookies,
        signer=BilibiliSign(img_key, sub_key),
    )


async def refresh_browser_session(
    context: BrowserContext,
    session: BrowserSession,
    client: httpx.AsyncClient,
) -> BrowserSession:
    """刷新浏览器会话，用现有 Cookie 重新读取登录态和 WBI 签名信息。"""
    return await build_browser_session(context, session.page, client, session.cookie_header)


async def fetch_creator_video_page(
    client: httpx.AsyncClient,
    creator_id: str,
    page_num: int,
    page_size: int,
    headers: dict[str, str],
    signer: BilibiliSign,
) -> dict[str, Any]:
    """请求某个 UP 主空间视频列表的单页原始数据。"""
    # 空间 WBI 接口要求签名参数，签名器会补充 w_rid 和 wts。
    params = signer.sign(
        {
            "mid": creator_id,
            "pn": page_num,
            "ps": page_size,
            "order": "pubdate",
        }
    )
    response = await client.get(
        f"{BILI_API}/x/space/wbi/arc/search",
        headers=headers,
        params=params,
    )
    response.raise_for_status()

    # B 站业务错误会放在 JSON code 中，HTTP 200 也可能代表抓取失败。
    payload = response.json()
    if payload.get("code") == -352:
        raise RiskControlError(payload)
    if payload.get("code") != 0:
        raise RuntimeError(f"creator videos api failed: {payload}")
    return payload.get("data", {})


async def fetch_page_with_retry(
    context: BrowserContext,
    session: BrowserSession,
    client: httpx.AsyncClient,
    config: CrawlConfig,
    creator_id: str,
    page_num: int,
) -> tuple[dict[str, Any], BrowserSession]:
    """带指数退避重试地抓取单页，并在失败后刷新浏览器会话。"""
    last_error: Exception | None = None
    current_session = session

    for attempt in range(1, config.retries + 1):
        # 每次重试都基于当前会话重新构造请求头，确保 Cookie 和签名器同步。
        headers = {
            "User-Agent": current_session.user_agent,
            "Referer": f"https://space.bilibili.com/{creator_id}",
            "Origin": BILI_HOME,
            "Cookie": current_session.cookie_header,
        }
        try:
            data = await fetch_creator_video_page(
                client=client,
                creator_id=creator_id,
                page_num=page_num,
                page_size=config.page_size,
                headers=headers,
                signer=current_session.signer,
            )
            return data, current_session
        except RiskControlError as error:
            last_error = error
            if attempt >= config.retries:
                break
            voucher_text = f", voucher={error.v_voucher}" if error.v_voucher else ""
            print(
                f"UID {creator_id} page {page_num} hit risk control on attempt "
                f"{attempt}/{config.retries}{voucher_text}. Cool down {config.risk_control_delay:.1f}s..."
            )
            await asyncio.sleep(config.risk_control_delay)
            # 风控后刷新会话，但避免立刻连续请求，先完成长冷却再重建签名信息。
            current_session = await refresh_browser_session(context, current_session, client)
        except (httpx.HTTPError, RuntimeError) as error:
            last_error = error
            if attempt >= config.retries:
                break
            # 使用指数退避，避免接口短暂异常时连续打满重试。
            delay = config.retry_delay * (2 ** (attempt - 1))
            print(
                f"UID {creator_id} page {page_num} failed on attempt {attempt}/{config.retries}: "
                f"{error}. Retry after {delay:.1f}s..."
            )
            await asyncio.sleep(delay)
            # 失败后重新进入首页刷新 Cookie/WBI key，再尝试下一轮请求。
            current_session = await refresh_browser_session(context, current_session, client)

    raise RuntimeError(f"UID {creator_id} page {page_num} failed after retries: {last_error}")


def simplify_video(raw_video: dict[str, Any]) -> dict[str, Any]:
    """把接口返回的视频对象压缩成输出文件需要的稳定字段。"""
    bvid = raw_video.get("bvid") or ""
    return {
        "aid": raw_video.get("aid"),
        "bvid": bvid,
        "title": raw_video.get("title"),
        "created": raw_video.get("created"),
        "length": raw_video.get("length"),
        "play": raw_video.get("play"),
        "comment": raw_video.get("comment"),
        "description": raw_video.get("description"),
        "is_union_video": bool(raw_video.get("is_union_video")),
        "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
    }


def new_output_document(config: CrawlConfig) -> dict[str, Any]:
    """创建新的输出 JSON 文档骨架，记录本次抓取配置和创作者列表。"""
    return {
        "generated_at": utc_now_text(),
        "updated_at": utc_now_text(),
        "config": {
            "max_pages": config.max_pages,
            "page_size": config.page_size,
            "sleep_seconds": config.sleep_seconds,
            "page_sleep_min": config.page_sleep_min,
            "page_sleep_max": config.page_sleep_max,
            "creator_sleep_min": config.creator_sleep_min,
            "creator_sleep_max": config.creator_sleep_max,
            "retries": config.retries,
            "retry_delay": config.retry_delay,
            "risk_control_delay": config.risk_control_delay,
            "request_timeout": config.request_timeout,
        },
        "creators": [],
    }


def load_output(path: pathlib.Path, config: CrawlConfig) -> dict[str, Any]:
    """加载已有输出用于续跑；禁用续跑或文件不存在时创建新文档。"""
    if not config.resume or not path.exists():
        return new_output_document(config)

    # 续跑时要求文件结构可识别，避免把不兼容文件继续写坏。
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    if not isinstance(payload, dict) or not isinstance(payload.get("creators"), list):
        raise ValueError(f"unsupported existing output format: {path}")
    payload["updated_at"] = utc_now_text()
    return payload


def get_creator_result(document: dict[str, Any], creator_id: str) -> dict[str, Any]:
    """在输出文档中查找或创建某个 UID 对应的抓取结果对象。"""
    for item in document["creators"]:
        if str(item.get("uid")) == creator_id:
            # 老文件可能缺少新增字段，续跑前补齐必要容器。
            item.setdefault("videos", [])
            item.setdefault("pages_crawled", [])
            return item

    # 首次抓取该 UID 时创建完整状态，后续每页都会增量更新。
    item = {
        "uid": creator_id,
        "started_at": utc_now_text(),
        "finished_at": None,
        "status": "pending",
        "total": None,
        "pages_crawled": [],
        "videos": [],
        "error": None,
    }
    document["creators"].append(item)
    return item


def merge_videos(existing: list[dict[str, Any]], new_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 bvid/aid 合并新旧视频列表，去重同时保留首次出现顺序。"""
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for item in existing + new_items:
        # 优先使用 bvid，其次 aid；都缺失时用顺序兜底避免覆盖。
        key = str(item.get("bvid") or item.get("aid") or len(order))
        if key not in merged:
            order.append(key)
        merged[key] = item

    return [merged[key] for key in order]


def atomic_write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    """把抓取结果原子写入 JSON 文件，降低中断导致文件损坏的风险。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = utc_now_text()

    # 先写同目录临时文件，再 replace 成目标文件，保证跨平台原子替换。
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
    ) as file:
        temp_path = pathlib.Path(file.name)
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")

    temp_path.replace(path)


def print_videos(creator_id: str, page_num: int, total: int | None, videos: list[dict[str, Any]]) -> None:
    """在终端打印单页抓取结果摘要，方便观察实时进度。"""
    print(f"\nUID {creator_id}, page {page_num}, got {len(videos)} videos, total={total}")
    for video in videos:
        # 接口 created 是秒级时间戳，这里转换成本地时间便于人工阅读。
        created_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(video["created"] or 0))
        union_text = "union" if video["is_union_video"] else "solo"
        print(f"- {video['bvid']} | {created_text} | {union_text} | {video['title']}")


async def crawl_one_creator(
    context: BrowserContext,
    session: BrowserSession,
    client: httpx.AsyncClient,
    config: CrawlConfig,
    document: dict[str, Any],
    creator_id: str,
) -> tuple[BrowserSession, bool]:
    """抓取单个 UP 主的视频页，边抓边保存，失败时记录错误状态。"""
    result = get_creator_result(document, creator_id)
    if config.resume and result.get("status") == "success":
        print(f"UID {creator_id} already finished. Skip creator.")
        return session, False

    # 先落盘 running 状态，使脚本中断后也能知道该 UID 上次处理到哪里。
    result["status"] = "running"
    result["error"] = None
    atomic_write_json(config.output, document)

    current_session = session
    pages_crawled = set(result.get("pages_crawled") or [])

    try:
        for page_num in range(1, config.max_pages + 1):
            # 续跑模式下跳过已经成功写入的页，避免重复请求。
            if config.resume and page_num in pages_crawled:
                print(f"UID {creator_id}, page {page_num} already exists. Skip.")
                continue

            data, current_session = await fetch_page_with_retry(
                context=context,
                session=current_session,
                client=client,
                config=config,
                creator_id=creator_id,
                page_num=page_num,
            )

            # 从接口响应中提取分页信息和视频列表，再统一整理输出字段。
            page_info = data.get("page", {})
            video_list = data.get("list", {}).get("vlist", [])
            total = page_info.get("count", result.get("total"))
            simplified = [simplify_video(item) for item in video_list]

            result["total"] = total
            result["videos"] = merge_videos(result["videos"], simplified)
            if page_num not in result["pages_crawled"]:
                result["pages_crawled"].append(page_num)
            result["last_page_finished_at"] = utc_now_text()

            # 每页完成后立即写文件，保证长任务中途退出也能续跑。
            print_videos(creator_id, page_num, total, simplified)
            atomic_write_json(config.output, document)

            # 没有更多视频，或当前页已经覆盖总数时提前结束该 UID。
            if not video_list or (total is not None and page_num * config.page_size >= int(total)):
                break
            await sleep_with_log("Wait before next page", config.page_sleep_min, config.page_sleep_max)

        result["status"] = "success"
        result["finished_at"] = utc_now_text()
    except Exception as error:
        # 单个 UID 失败不抛出到总流程，记录错误后继续处理后续 UID。
        result["status"] = "failed"
        result["finished_at"] = utc_now_text()
        result["error"] = str(error)
        atomic_write_json(config.output, document)
        print(f"UID {creator_id} failed: {error}")

    atomic_write_json(config.output, document)
    return current_session, True


async def crawl_creators(config: CrawlConfig) -> None:
    """启动浏览器和 HTTP 客户端，按配置顺序批量抓取多个 UP 主。"""
    config.user_data_dir.mkdir(parents=True, exist_ok=True)
    document = load_output(config.output, config)

    # httpx 负责 API 请求，Playwright 负责登录态、Cookie 和浏览器环境。
    timeout = httpx.Timeout(config.request_timeout)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(config.user_data_dir),
                headless=config.headless,
                viewport={"width": 1280, "height": 900},
                user_agent=DEFAULT_USER_AGENT,
            )

            try:
                page = await context.new_page()
                # 整个批次复用同一浏览器会话，必要时抓取页内部会刷新会话。
                session = await build_browser_session(context, page, client, config.cookie)

                for index, creator_id in enumerate(config.creator_ids, start=1):
                    print(f"\nStart UID {creator_id} ({index}/{len(config.creator_ids)})")
                    session, did_process = await crawl_one_creator(
                        context=context,
                        session=session,
                        client=client,
                        config=config,
                        document=document,
                        creator_id=creator_id,
                    )
                    if did_process and index < len(config.creator_ids):
                        # 不同 UID 之间也做间隔，降低请求过于密集的概率。
                        await sleep_with_log(
                            "Wait before next creator",
                            config.creator_sleep_min,
                            config.creator_sleep_max,
                        )
            finally:
                await close_browser_context_safely(context)


def main() -> None:
    """脚本入口：解析配置并运行异步抓取主流程。"""
    config = build_config(parse_args())
    try:
        asyncio.run(crawl_creators(config))
    except KeyboardInterrupt:
        print("\nInterrupted by user. Progress already written to output JSON after each finished page.")


if __name__ == "__main__":
    main()
