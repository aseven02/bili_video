from playwright.sync_api import sync_playwright
from datetime import datetime
import json


CLASS_LIST = [
    "douga", "game", "kichiku", "music", "dance",
    "cinephile", "ent", "knowledge", "tech",
    "food", "car", "fashion", "sports", "animal"
]


def get_href(locator):
    """获取完整链接"""
    href = locator.get_attribute("href")
    return f"https:{href}" if href else None


def get_info_from_class(page, class_name):
    """根据分类获取排行榜信息"""

    target_url = f"https://www.bilibili.com/v/popular/rank/{class_name}"
    print(f"\n正在分析分类：{class_name}")

    try:
        page.goto(target_url, wait_until="domcontentloaded")
        page.wait_for_selector("div.info", timeout=10000)

    except Exception as e:
        print(f"页面加载失败：{e}")
        return []

    results = []

    item_list = page.locator("div.info")
    count = item_list.count()

    print(f"共获取到 {count} 条视频")

    for i in range(count):

        item = item_list.nth(i)

        links = item.locator("a")

        if links.count() < 2:
            print(f"第 {i + 1} 条数据结构异常，跳过")
            continue

        video_info = links.nth(0)
        up_info = links.nth(1)

        results.append(
            {
                "video_name": video_info.inner_text().strip(),
                "video_url": get_href(video_info),
                "up_name": up_info.inner_text().strip(),
                "up_url": get_href(up_info),
            }
        )

    return results


def main():

    final_results = {}

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
        )

        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
        )

        for class_name in CLASS_LIST:
            result = get_info_from_class(page, class_name)
            final_results[class_name] = result

        browser.close()

    date_str = datetime.now().strftime("%Y%m%d")
    output_file = f"bili_rank_data/bilibili_info_{date_str}.json"

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(final_results, f, ensure_ascii=False, indent=4)

    print(f"\n数据已保存到：{output_file}")


if __name__ == "__main__":
    main()