import httpx
import json
from bili_sign_test import BilibiliSign, BrowserSession, build_browser_session
from playwright.sync_api import sync_playwright, Page



def load_video(session, client, bvid, params, video_path):
    """加载视频播放页，获取视频信息和下载链接。"""
    url = f"https://api.bilibili.com/x/player/wbi/playurl"

    headers = {
            "User-Agent": session.user_agent,
            "Referer": f"https://www.bilibili.com/video/{bvid}",
            "Origin": "https://www.bilibili.com",
            "Cookie": session.cookie_header,
        }
    response = client.get(url, params=params, headers=headers)    
    response.raise_for_status()
    data = response.json()

    # with open('test/output/bili_video_playurl_response_4048.json','w', encoding='utf-8') as f:
    #     json.dump(data, f, ensure_ascii=False, indent=4)

    # 这里是1的url地址，4048的不一样
    playurl = data.get("data", {}).get("durl", [])[0].get("url", [])

    print(f"playurl: {playurl}")
    video_response = client.get(playurl, headers=headers)
    video_response.raise_for_status()
    with open(f"{video_path}.mp4", "wb") as f:
        f.write(video_response.content)
    


def get_video_info(client, bvid):
    """获取视频信息,包括aid和cid"""
    params = {"bvid": bvid}
    url = f"https://api.bilibili.com/x/web-interface/view"
    headers = {
        "User-Agent": 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36'
    }
    result = client.get(url, params=params, headers=headers)
    result.raise_for_status()
    data = result.json()
    return {
        'aid': data['data']['aid'],
        'cid': data['data']['cid'],

    }


with sync_playwright() as playwright:
    context = playwright.chromium.launch_persistent_context(user_data_dir="./user_data", headless=False, channel="chrome")
    page = context.new_page()
    
    with httpx.Client(follow_redirects=True) as client:
        session = build_browser_session(context, page, client)

        target_bvid = 'BV1BHtR69EtS'
        video_info = get_video_info(client, target_bvid)
        params = {
            "avid": video_info['aid'],
            "cid": video_info['cid'],
            "qn": 80,
            "fourk": 1,
            "fnval": 1, ## 1 or 4048
            "platform": "pc",
        }
        signed_params = session.signer.sign(params)
        load_video(session, client, target_bvid, signed_params, 'test/output/bili_video_download_test.mp4')