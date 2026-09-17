from dataclasses import dataclass
from playwright.sync_api import Page, BrowserContext, sync_playwright
import httpx
import time
import urllib.parse
from hashlib import md5


class BilibiliSign:
    """Sign Bilibili WBI request params."""

    _MIXIN_KEY_ENC_TAB = [
        46,
        47,
        18,
        2,
        53,
        8,
        23,
        32,
        15,
        50,
        10,
        31,
        58,
        3,
        45,
        35,
        27,
        43,
        5,
        49,
        33,
        9,
        42,
        19,
        29,
        28,
        14,
        39,
        12,
        38,
        41,
        13,
        37,
        48,
        7,
        16,
        24,
        55,
        40,
        61,
        26,
        17,
        0,
        1,
        60,
        51,
        30,
        4,
        22,
        25,
        54,
        21,
        56,
        59,
        6,
        63,
        57,
        62,
        11,
        36,
        20,
        34,
        44,
        52,
    ]

    def __init__(self, img_key: str, sub_key: str) -> None:
        if not img_key or not sub_key:
            raise ValueError("img_key and sub_key are required")
        self._mixin_key = self._make_mixin_key(img_key + sub_key)

    @classmethod
    def _make_mixin_key(cls, key: str) -> str:
        if len(key) <= max(cls._MIXIN_KEY_ENC_TAB):
            raise ValueError("WBI key is too short")
        return "".join(key[index] for index in cls._MIXIN_KEY_ENC_TAB)[:32]

    def sign(self, params: dict) -> dict[str, str]:
        signed_params = {**params, "wts": int(time.time())}
        signed_params = dict(sorted(signed_params.items()))
        filtered_params = {
            key: "".join(ch for ch in str(value) if ch not in "!'()*")
            for key, value in signed_params.items()
        }
        query = urllib.parse.urlencode(filtered_params)
        filtered_params["w_rid"] = md5((query + self._mixin_key).encode()).hexdigest()
        return filtered_params



@dataclass(frozen=True)
class BrowserSession:
    """保存浏览器态衍生出的请求信息，包括 Cookie、UA 和 WBI 签名器。"""

    page: Page
    user_agent: str
    cookie_header: str
    signer: BilibiliSign


BILI_HOME = "https://www.bilibili.com"
BILI_API = "https://api.bilibili.com"

def cookie_header_from_playwright(cookies) -> str:
    return "; ".join(f"{item['name']}={item['value']}" for item in cookies) if cookies else ""



def get_nav_data(client: httpx.Client, headers: dict[str, str]) -> dict:
    """调用 B 站导航接口，获取登录状态和 WBI 图片信息等全局数据。"""
    response = client.get(f"{BILI_API}/x/web-interface/nav", headers=headers)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        print("Failed to get nav data:", payload)
    return payload.get("data", {})


def get_wbi_keys(page: Page, client: httpx.Client, headers: dict[str, str]) -> tuple[str, str]:
    """优先从 localStorage 读取 WBI key,失败时回退到 nav 接口。"""
    # 优先读页面 localStorage，减少一次网络请求，也复用浏览器当前状态。
    local_storage =  page.evaluate("() => ({ ...window.localStorage })")
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
        nav_data = get_nav_data(client, headers)
        wbi_img = nav_data.get("wbi_img") or {}
        img_url = wbi_img.get("img_url", "")
        sub_url = wbi_img.get("sub_url", "")

    if not img_url or not sub_url:
        raise RuntimeError("failed to load WBI keys from browser localStorage or nav API")

    # WBI 签名只需要图片文件名中的 key，不需要完整 URL。
    img_key = img_url.rsplit("/", 1)[1].split(".", 1)[0]
    sub_key = sub_url.rsplit("/", 1)[1].split(".", 1)[0]
    return img_key, sub_key


def wait_for_login(context: BrowserContext, page: Page) -> str:
    """没有可用缓存登录态时，等待用户手动登录并返回新的 Cookie 请求头。"""
    for _ in range(180):
        # 每秒读取一次浏览器 Cookie，出现登录 Cookie 就继续后续 API 抓取。
        raw_cookies = context.cookies([BILI_HOME])
        cookie_names = set(item["name"] for item in raw_cookies)
        if "SESSDATA" in cookie_names or "DedeUserID" in cookie_names:
                    return cookie_header_from_playwright(raw_cookies)
        page.wait_for_timeout(1000)

    raise RuntimeError("Login timed out. Please scan/login in the opened browser, or pass --cookie.")


def build_browser_session(
    context: BrowserContext,
    page: Page,
    client: httpx.Client,
) :
    """初始化浏览器会话，确认登录态并创建后续 API 请求需要的 WBI 签名器。"""
    # 先把命令行 Cookie 注入浏览器，再打开首页触发站点初始化逻辑。
    page.goto(BILI_HOME, wait_until="domcontentloaded")

    # 检查浏览器中的登录状态, 获取浏览器 UA 和 Cookie, 构建后续 httpx 请求的 headers
    user_agent = page.evaluate("() => navigator.userAgent")
    raw_cookies = context.cookies([BILI_HOME])

    # 检查浏览器 Cookie 中是否有登录态，如果没有就提示用户手动登录
    cookie_names = set(item["name"] for item in raw_cookies)
    if "SESSDATA" not in cookie_names and "DedeUserID" not in cookie_names:
        print("No valid login cookies found in the browser. Please login manually in the opened browser window...")
        cookies = wait_for_login(context, page)
    else:
        cookies = cookie_header_from_playwright(raw_cookies)

    headers = {
        "User-Agent": user_agent,
        "Referer": BILI_HOME,
        "Origin": BILI_HOME,
        "Cookie": cookies,
    }

    # WBI key 会参与空间视频接口签名，必须在正式抓取前准备好。
    img_key, sub_key = get_wbi_keys(page, client, headers)
    return BrowserSession(
        page=page,
        user_agent=user_agent,
        cookie_header=cookies,
        signer=BilibiliSign(img_key, sub_key),
    )