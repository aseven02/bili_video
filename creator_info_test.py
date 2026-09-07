import requests
import json
import httpx
from tqdm import tqdm

def get_mid_list(filepath, n=100):
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data[:n]


def get_mid_detail(mid):
    url = "https://api.bilibili.com/x/web-interface/card"
    params = {
        "mid": mid,
        "photo": True
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/",
        "Referer": "https://www.bilibili.com/",
    }

    response = httpx.get(url, params=params, headers=headers, timeout=15)
    result = response.json()
    if result["code"] == 0:
        print(f"{mid} success!")
    else:
        print(f"{mid} failed")
        return None
    data = result.get("data", {})
    card = data.get("card", {})
    official_raw = card.get("Official") or card.get("official") or card.get("official_verify")
    return {
        "uid": str(card.get("mid", "")),
        "name": card.get("name", ""),
        "face": card.get("face", ""),
        "fans": data.get("follower") if data.get("follower") is not None else card.get("fans"),
        "like_num": data.get("like_num"),
        "level": card.get("level_info", {}).get("current_level"),
        "sign": card.get("sign"),
        "description": card.get("description") or card.get("sign"),
        "official": {
            "type": official_raw.get("type") if official_raw.get("type") is not None else official_raw.get("role"),
            "title": official_raw.get("title"),
            "description": official_raw.get("desc") or official_raw.get("description"),
        },
        "archive_count": data.get("archive_count"),
        "article_count": data.get("article_count"),
        "space_url": f"https://space.bilibili.com/{card.get('mid', '')}",
    }
final_result = []

for mid in tqdm(get_mid_list("creator_batches/uid_batch_0.json", n=50)):
    result = get_mid_detail(mid)
    if not result:
        print(f"Failed to get result for {mid}")
    final_result.append(result)

with open('creator_info_test.json', 'w', encoding='utf-8') as f:
    json.dump(final_result, f, ensure_ascii=False, indent=4)