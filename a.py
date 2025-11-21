import asyncio
import json
import os
from playwright.async_api import async_playwright
import requests
import time

BOT_TOKEN = "8142781290:AAFM6d0H4Cv4f1YIZkvbQAOON1shB0L0QHg"

# Danh sách group để gửi tin – bạn có thể thêm group tại đây hoặc tự động load từ file
GROUPS_FILE = "telegram_groups.json"

USERNAMES = ["elonmusk", "nasa", "tommyzz8"]

STATE_FILE = "tweet_state.json"
COOKIES_JSON = "twitter_cookies.json"


# ---------------------------
# FILE FUNCTIONS
# ---------------------------

def load_json(path, default):
    if os.path.exists(path):
        try:
            return json.load(open(path, "r", encoding="utf-8"))
        except:
            return default
    return default


def save_json(path, data):
    json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def load_state():
    return load_json(STATE_FILE, {})


def save_state(state):
    save_json(STATE_FILE, state)


def load_groups():
    return load_json(GROUPS_FILE, [])


def save_groups(groups):
    save_json(GROUPS_FILE, groups)


# ---------------------------
# TELEGRAM FUNCTIONS
# ---------------------------

def send_telegram(msg):
    """Gửi vào tất cả các group đã lưu"""
    groups = load_groups()

    for gid in groups:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": gid,
            "text": msg,
            "disable_web_page_preview": True
        })


def check_new_groups():
    """Quét getUpdates để lấy group mới bot được add vào"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    res = requests.get(url).json()

    if not res.get("ok"):
        return

    groups = load_groups()
    changed = False

    for update in res["result"]:

        # Kiểm tra bot được add vào group qua my_chat_member
        if "my_chat_member" in update:
            chat = update["my_chat_member"]["chat"]
            status = update["my_chat_member"]["new_chat_member"]["status"]

            if status in ["member", "administrator"]:
                gid = chat["id"]

                # nhóm thì id < 0
                if isinstance(gid, int) and gid < 0 and gid not in groups:
                    groups.append(gid)
                    changed = True
                    print("📌 Bot được add vào group:", gid)

    if changed:
        save_groups(groups)


# ---------------------------
# TWITTER API CRAWL (SIÊU ỔN ĐỊNH)
# ---------------------------

async def load_cookies(context):
    if not os.path.exists(COOKIES_JSON):
        print("❌ Chưa login — hãy chạy login.py trước")
        return False

    cookies = json.load(open(COOKIES_JSON))
    await context.add_cookies(cookies)
    return True


async def get_latest_tweet(page, username):
    """Bắt API tweet thay vì đọc DOM (ổn định 99.99%)"""

    api_response = None

    def handle_response(response):
        nonlocal api_response
        url = response.url

        # API tweets của Twitter luôn có "UserTweets"
        if "UserTweets" in url and "UserByScreenName" not in url:
            api_response = response

    page.on("response", handle_response)

    await page.goto(f"https://x.com/{username}", timeout=60000)

    # Chờ tối đa 10s để API trả về dữ liệu
    for _ in range(20):
        if api_response:
            break
        await asyncio.sleep(0.5)

    if not api_response:
        print(f"⚠️ Không bắt được API tweet cho {username}")
        return None

    try:
        json_data = await api_response.json()
        print(json_data)
    except:
        print(f"⚠️ Lỗi đọc JSON API tweet cho {username}")
        return None

    # Parse JSON tweet
    try:
        instructions = json_data["data"]["user"]["result"]["timeline_v2"]["timeline"]["instructions"]

        for ins in instructions:
            if ins["type"] == "TimelineAddEntries":
                for entry in ins["entries"]:
                    if entry["entryId"].startswith("tweet-"):

                        tweet = entry["content"]["itemContent"]["tweet_results"]["result"]

                        tweet_id = tweet["legacy"]["id_str"]
                        text = tweet["legacy"]["full_text"]
                        link = f"https://twitter.com/{username}/status/{tweet_id}"

                        return tweet_id, text, link

    except Exception as e:
        print("⚠️ Lỗi parse JSON tweet:", e)

    return None


# ---------------------------
# MAIN PROGRAM
# ---------------------------

async def main():
    state = load_state()
    check_new_groups()

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
                    print("⚠️ Không lấy được tweet:", username)
                    continue

                tweet_id, text, link = data

                if state.get(username) != tweet_id:
                    msg = f"📢 @{username} vừa đăng tweet:\n\n{text}\n\n{link}"
                    send_telegram(msg)
                    state[username] = tweet_id

            except Exception as e:
                print("❌ Lỗi:", e)

        save_state(state)
        await browser.close()


if __name__ == "__main__":
    while True:
        asyncio.run(main())
        print("✔ done, chờ 60s")
        time.sleep(60)
