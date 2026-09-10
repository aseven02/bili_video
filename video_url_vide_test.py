import httpx
import json
def get_request_result(bvid):
    url = "https://api.bilibili.com/x/web-interface/view"
    params = {
        "bvid": bvid,
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/",
        "Referer": "https://www.bilibili.com/",
    }

    response = httpx.get(url, params=params, headers=headers, timeout=15)
    print(response.text)
    result = response.json()
    return result

bvid = "BV1Zt4y1C7gD"
result = get_request_result(bvid)

with open('video_url_view_test.json', 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=4)