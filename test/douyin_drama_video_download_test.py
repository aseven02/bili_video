import httpx

video_url =\
"https://www.douyin.com/aweme/v1/play/?video_id=v0300fg10000d9v8lqvog65vp85l3790\u0026line=0\u0026file_id=6784a906699644fda8ecec23f1d32b2c\u0026sign=fd9ec6ba34f15e0b80133f4e519853bd\u0026is_play_url=1\u0026source=PackSourceEnum_SERIES_AWEME\u0026biz_sign=NRPI1lC4AlkgB1YZ_o3JxvAfESRKvePEcuaasIY4ZWuwTcZpH4kJxcZEpe2E9VaaOzLa7CA0Ou-V1V2BgVcbed4S6EvCDGbNHaUZIZuaRMUGyO7s5wbVW7et0I2xs1hA\u0026aid=6383"
video_url = video_url.replace(r"\u0026", "&")

test_url = \
"https://www.douyin.com/aweme/v1/play/?video_id=v0300fg10000d9v8lqvog65vp85l3790\u0026line=0\u0026file_id=efa6566d042740bb8bdc91d4285c6804\u0026sign=27df8786932a6839ab4cb4fcf1e53bb9\u0026is_play_url=1\u0026source=PackSourceEnum_SERIES_AWEME\u0026biz_sign=NRPI1lC4AlkgB1YZ_o3JxvAfESRKvePEcuaasIY4ZWuwTcZpH4kJxcZEpe2E9VaaOzLa7CA0Ou-V1V2BgVcbed4S6EvCDGbNHaUZIZuaRMUGyO7s5wbVW7et0I2xs1hA\u0026aid=6383"
test_url = test_url.replace(r"\u0026", "&")

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/153.0.0.0 Safari/537.36"
    ),
    # 这个必须加，不然会返回 403 Forbidden
    "Referer": "https://www.douyin.com/",
}

with httpx.Client(
    headers=headers,
    follow_redirects=True,
    timeout=20
) as client:

    response = client.get(video_url)

    print("status:", response.status_code)
    print("type:", response.headers.get("content-type"))
    print("final URL:", response.url)
    print("size:", len(response.content))

    response.raise_for_status()

    with open("test/output/douyin_drama_test.mp4", "wb") as f:
        f.write(response.content)