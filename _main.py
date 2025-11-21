import asyncio
import json
import os
from playwright.async_api import async_playwright
import requests
import time

BOT_TOKEN = "8142781290:AAFM6d0H4Cv4f1YIZkvbQAOON1shB0L0QHg"
CHAT_ID = "-5093959409"

USERNAMES = ["elonmusk", "nasa", "tommyzz8"]

STATE_FILE = "tweet_state.json"
COOKIES_JSON = "twitter_cookies.json"


def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE))
    return {}

def save_state(state):
    json.dump(state, open(STATE_FILE, "w"))


def send_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": CHAT_ID,
        "text": msg,
        "disable_web_page_preview": True
    })


async def load_cookies(context):
    if not os.path.exists(COOKIES_JSON):
        print("❌ Chưa login — hãy chạy login.py trước")
        return False

    cookies = json.load(open(COOKIES_JSON))
    await context.add_cookies(cookies)
    return True


async def get_latest_tweet(page, username):
    url = f"https://x.com/{username}"
    await page.goto(url, timeout=60000)
    await page.wait_for_timeout(4000)

    # Nếu dính private account mà bạn không follow → sẽ thấy yêu cầu follow
    if await page.query_selector("div[data-testid='emptyState']"):
        return None

    tweet = await page.query_selector("a[href*='/status/']")
    if not tweet:
        return None

    link = await tweet.get_attribute("href")
    tweet_id = link.split("/")[-1]

    text_el = await page.query_selector("div[data-testid='tweetText']")
    text = await text_el.inner_text() if text_el else ""

    return tweet_id, text, "https://twitter.com" + link


async def main():
    state = load_state()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()

        ok = await load_cookies(context)
        if not ok:
            return

        page = await context.new_page()
        await page.goto("https://x.com/home")

        if "login" in page.url.lower():
            print("❌ Cookie hết hạn — hãy chạy login.py để login lại")
            return

        for username in USERNAMES:
            try:
                data = await get_latest_tweet(page, username)
                if not data:
                    print("Không đọc được tweet cho", username)
                    continue

                tweet_id, text, link = data

                if state.get(username) != tweet_id:
                    send_telegram(f"📢 @{username} vừa đăng tweet:\n\n{text}\n\n{link}")
                    state[username] = tweet_id

            except Exception as e:
                print("Lỗi:", e)

        save_state(state)
        await browser.close()


if __name__ == "__main__":
    while True:
        asyncio.run(main())
        print("✔ done, chờ 60s")
        time.sleep(1)
