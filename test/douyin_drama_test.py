import httpx
import json
from pathlib import Path

url = f'https://www.douyin.com/aweme/v1/web/series/aweme/'
url2 = f'https://www.douyin.com/aweme/v1/web/series/detail'

headers = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Referer': 'https://www.douyin.com/',
    'cookie' : f'enter_pc_once=1; UIFID_TEMP=a9feeece8bad9a3ec4489a53034d3a9cea9eedcaf883b88008ea0decba55736a6e54d4d6d4a1b04133846b273c76e23812946586976e0eb3493daf361b2a58ece0e43dc19a3bf660c47b0ca2d998d726; s_v_web_id=verify_mtuyqn4u_TFYWMJUh_M0ms_4NoA_8BjV_iw0YGFfSemWK; hevc_supported=true; dy_swidth=2560; dy_sheight=1440; odin_tt=1d5333efbbf7f9112c8cace058dc73c5127baa7d919cffdff522c5725c2d0690d1373afb14bf24d32b9d88e2da52601c6f3209d0c7430b0b53b71a6ad28666e10966811dc2e772e73064132296e6b105; fpk1=U2FsdGVkX1+v+R5ScK0IyIKSIcMlSIjzb1eBlqlZK5G+nOuiIKfLuS7vZOlEbK+xJY0Lamt1dZ2rj40bw0EXOQ==; fpk2=b8686dc00110378d4f62d0eaf9196000; passport_csrf_token=9717694d2229cad038ff8294f49c7ad7; passport_csrf_token_default=9717694d2229cad038ff8294f49c7ad7; __security_mc_1_s_sdk_crypt_sdk=5485b07c-4c71-b32c; bd_ticket_guard_regenerate_keys_time=2026-09-10/11:23:56; bd_ticket_guard_client_web_domain=2; is_dash_user=1; UIFID=a9feeece8bad9a3ec4489a53034d3a9cea9eedcaf883b88008ea0decba55736a6e54d4d6d4a1b04133846b273c76e238c16c98b3966a762c183e7dfc4e9d7d7b5446fec94d93043498790bd54ea22f8c547cf7f5f4b0fce1159f6dabd40f08c2c134fb0e002b9c454ba2ec7fe000d6d1b2e64b6a52c326e77053247623fed17a1323d7fa02ddfbcd0f058bf68e28b897160635c454b6571feb101f573d8ef1f4; download_guide=%223%2F20260910%2F0%22; volume_info=%7B%22isUserMute%22%3Afalse%2C%22isMute%22%3Atrue%2C%22volume%22%3A0.5%7D; douyin.com; device_web_cpu_core=8; device_web_memory_size=32; is_support_rtm_web_ts=1; stream_recommend_feed_params=%22%7B%5C%22cookie_enabled%5C%22%3Atrue%2C%5C%22screen_width%5C%22%3A2560%2C%5C%22screen_height%5C%22%3A1440%2C%5C%22browser_online%5C%22%3Atrue%2C%5C%22cpu_core_num%5C%22%3A8%2C%5C%22device_memory%5C%22%3A32%2C%5C%22downlink%5C%22%3A1.65%2C%5C%22effective_type%5C%22%3A%5C%224g%5C%22%2C%5C%22round_trip_time%5C%22%3A50%7D%22; strategyABtestKey=%221789093960.116%22; bd_ticket_guard_client_data=eyJiZC10aWNrZXQtZ3VhcmQtdmVyc2lvbiI6MiwiYmQtdGlja2V0LWd1YXJkLWl0ZXJhdGlvbi12ZXJzaW9uIjoxLCJiZC10aWNrZXQtZ3VhcmQtcmVlLXB1YmxpYy1rZXkiOiJCRndIaDNRWllsbituZW1uaU1mcnh4WVM4aFlBVkMwZGo1UTU3dW9JdzhyMFdxSGFoTlpNQnp4bWt6aWllSnZRN0JJNEE5Y2hydzNlZG12S0ZEalU1UEE9IiwiYmQtdGlja2V0LWd1YXJkLXdlYi12ZXJzaW9uIjoyfQ%3D%3D; home_can_add_dy_2_desktop=%221%22; ttwid=1%7CC4G3bAN0FQBZNZ5CwjIXEGL1rFnB5It2dcdHgQoqbmI%7C1789093970%7Cbba0ffb218539b2d5bb375d2fec30577b1a35e22d5046f8923166d3d7762c561; biz_trace_id=fb31bd62; IsDouyinActive=true'
}
params = [
    # 基础公共参数
    ("device_platform", "webapp"),
    ("aid", "6383"),
    ("channel", "channel_pc_web"),

    # 接口业务参数
    ("series_id", 7673723557569890356),
    # ("pull_type", "2"),
    # ("cursor", "0"),
    # ("count", "12"),

    # 会话和风控参数
    ("webid", '7683742127989982754'),
    ("uifid", 'a9feeece8bad9a3ec4489a53034d3a9cea9eedcaf883b88008ea0decba55736a6e54d4d6d4a1b04133846b273c76e238c16c98b3966a762c183e7dfc4e9d7d7b5446fec94d93043498790bd54ea22f8c547cf7f5f4b0fce1159f6dabd40f08c2c134fb0e002b9c454ba2ec7fe000d6d1b2e64b6a52c326e77053247623fed17a1323d7fa02ddfbcd0f058bf68e28b897160635c454b6571feb101f573d8ef1f4'),
    ("msToken", 'zGExSHeuv4dnY1R__9aL6Qixqg15iSKu0oJ1nCw2MxFGvUUUFpVgcxgeP_xgNdDA1h1OvJqANT9fHjvn-DW72JrMc2YI8dHrzJ47lPpU2qZrY6UL7Hqa__qNPGuV-DlIJA38CM0hEvb6-v0I3WxRkbw4kn9d395vW-vbEtZJgufJYMgsHuQNgQ=='),
    ("a_bogus", 'xJ0VkzXwEdQfFdKSuOOnSvclT1flNT8yrFTobT/TCPYNywlbOYPHKaeqGozJWc8GGRpTh9A7znalYjdbNUUipeHkLmpfuNtba0I99zfo2HkZGPvg3H6ZC7uFqXBYUcJL-AVRiIDlhUe7ZVV-hqQm/BIHtCje5mWhOZxRk2zCi9GgZKuIdZZhiM0gyfn9BB5dsHS='),
    ("verifyFp", 'verify_mtuyqn4u_TFYWMJUh_M0ms_4NoA_8BjV_iw0YGFfSemWK'),
    ("fp", 'verify_mtuyqn4u_TFYWMJUh_M0ms_4NoA_8BjV_iw0YGFfSemWK' ),
]

with httpx.Client(headers=headers, follow_redirects=True, timeout=20.0) as client:
    response = client.get(url2, params=params)

print('status:', response.status_code)
print('content-type:', response.headers.get('content-type'))
print('server:', response.headers.get('server'))
print('request parameter count:', len(params))
print('response preview:', response.text[:500])
response.raise_for_status()

if 'application/json' in response.headers.get('content-type', ''):
    result = response.json()
    output_path = Path(__file__).parent / 'output' / 'douyin_detail_test.json'
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('saved:', output_path)
