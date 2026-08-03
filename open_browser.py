from playwright.sync_api import sync_playwright


with sync_playwright() as p:

    context = p.chromium.launch_persistent_context(
        user_data_dir="./chrome_data",
        headless=False
    )

    page = context.new_page()

    page.goto(
        "https://www.bilibili.com"
    )
    input("Press Enter after logging in...")

    context.close()