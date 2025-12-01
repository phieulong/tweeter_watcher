import asyncio
from playwright.async_api import async_playwright
import json

COOKIES_JSON = "cookies/twitter_cookies_2.json"

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, args=[
            "--disable-blink-features=AutomationControlled",
            "--start-maximized"
        ])

        context = await browser.new_context()
        page = await context.new_page()

        print("👉 Đang mở Twitter...")
        await page.goto("https://x.com/login", timeout=60000)

        print("\n======================================")
        print(" 👉 HÃY ĐĂNG NHẬP BẰNG TAY TRONG CỬA SỔ HIỆN RA")
        print(" 👉 SAU KHI LOGIN THÀNH CÔNG VÀ THẤY TRANG HOME → quay lại terminal và nhấn ENTER")
        print("======================================\n")

        input("Nhấn ENTER sau khi login hoàn tất...")

        # Lưu cookie
        cookies = await context.cookies()
        json.dump(cookies, open(COOKIES_JSON, "w"), indent=2)

        print(f"✔ Đã lưu cookie vào {COOKIES_JSON}")
        await browser.close()

asyncio.run(main())