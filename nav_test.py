import httpx
import requests
from playwright.sync_api import sync_playwright

def playwright_get_request_result():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto("https://www.bilibili.com/")
        result = page.evaluate(
                    """async () => {
                        const response = await fetch(
                            "https://api.bilibili.com/x/web-interface/nav",
                            {
                                method: "GET",
                                credentials: "include"
                            }
                        );

                        const text = await response.text();

                        return {
                            status: response.status,
                            contentType:
                                response.headers.get("content-type") || "",
                            text
                        };
                    }"""
                )
    print(result)

def httpx_get_request_result():
    url = "https://api.bilibili.com/x/web-interface/nav"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/",
        "Referer": "https://www.bilibili.com/",
    }
    response = httpx.get(url, headers=headers, timeout=15)
    result = response.json()
    print(result)

httpx_get_request_result()