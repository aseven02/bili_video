import httpx
import json

def get_bvids_list(filepath, n=100):
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    bvid = [video for item in data for video in item['videos']]
    return bvid[:n]

def get_request_result(bvid):
    url = "https://api.bilibili.com/x/web-interface/view?"
    params = {
        "bvid": bvid,
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/",
        "Referer": "https://www.bilibili.com/",
    }

    response = httpx.get(url, params=params, headers=headers, timeout=15)
    result = response.json()

    if result["code"] == 0:
        video_data = {
            'bvid': bvid,
            'title': result.get("data", {}).get("title"),
            'pic': result.get("data", {}).get("pic"),
            'pubdate': result.get("data", {}).get("pubdate"),
            'desc': result.get("data", {}).get("desc"),
            'duration': result.get("data", {}).get("duration"),
            'owner': result.get("data", {}).get("owner", {}),
            'stat': result.get("data", {}).get("stat", {}),
            'argue_info': result.get("data", {}).get("argue_info", {}),
            'is_cooperation': result.get("data", {}).get("rights", {}).get("is_cooperation"),
        }
        if result.get("data", {}).get("staff"):
            video_data['staff'] = [
                {
                    'mid': staff.get("mid"),
                    'title': staff.get("title"),
                    'name': staff.get("name"),
                    'face': staff.get("face"),
                    'follower': staff.get("follower"),
                }
                for staff in result.get("data", {}).get("staff", [])
            ]
        else:
            video_data['staff'] = []
        return video_data
    else:
        print(f"{bvid} failed")
        return None

bvid = get_bvids_list("video_batches/video_batches_0.json", n=100)
result_list = []
for b in bvid:
    result = get_request_result(b)
    if not result:
        print(f"Failed to get result for {b}")
        break
    result_list.append(result)
with open('video_url_test.json', 'w', encoding='utf-8') as f:
    json.dump(result_list, f, ensure_ascii=False, indent=4)
print(f"Successfully processed {len(result_list)} videos.")
