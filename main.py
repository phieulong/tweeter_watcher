import asyncio
import json
import os
from playwright.async_api import async_playwright
import requests
import time
import http.server
import threading
import atexit


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    # silence logs from http.server
    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.environ.get("PORT", "8080"))
    # Use ThreadingHTTPServer so each health check runs in its own thread
    try:
        http.server.ThreadingHTTPServer.allow_reuse_address = True
        server = http.server.ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    except OSError as e:
        print(f"Failed to start health server on port {port}: {e}")
        return None

    # serve in a daemon thread
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Health server running on 0.0.0.0:{port}/healthz")

    # ensure server is shut down cleanly on process exit
    def _cleanup():
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass

    atexit.register(_cleanup)
    return server


# start health server early so Fly health checks succeed
_health_server = None
try:
    _health_server = start_health_server()
except Exception as e:
    print("Warning: cannot start health server:", e)

BOT_TOKEN = "8142781290:AAFM6d0H4Cv4f1YIZkvbQAOON1shB0L0QHg"
USERNAMES = ["elonmusk", "tommyzz8", "nhat1122319"]

STATE_FILE = "tweet_state.json"
COOKIES_JSON = "twitter_cookies.json"
GROUP_FILE = "groups.json"
OFFSET_FILE = "update_offset.json"


# =========================================
# GROUP MANAGEMENT
# =========================================

def load_groups():
    if not os.path.exists(GROUP_FILE):
        return []
    return json.load(open(GROUP_FILE))


def save_groups(groups):
    json.dump(groups, open(GROUP_FILE, "w"))


def load_offset():
    if not os.path.exists(OFFSET_FILE):
        return 0
    return json.load(open(OFFSET_FILE))


def save_offset(offset):
    json.dump(offset, open(OFFSET_FILE, "w"))


def check_new_groups():
    """Dùng getUpdates để xem bot được add vào group mới"""
    offset = load_offset()

    res = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
        params={"offset": offset, "timeout": 0}
    ).json()

    if not res.get("ok"):
        return

    groups = load_groups()
    bot_id = int(BOT_TOKEN.split(":")[0])

    for update in res["result"]:
        update_id = update["update_id"]
        save_offset(update_id + 1)

        # --- TRƯỜNG HỢP BOT ĐƯỢC ADD BẰNG my_chat_member ---
        if "my_chat_member" in update:
            chat = update["my_chat_member"]["chat"]
            old_status = update["my_chat_member"]["old_chat_member"]["status"]
            new_status = update["my_chat_member"]["new_chat_member"]["status"]

            if old_status == "left" and new_status in ["member", "administrator"]:
                chat_id = chat["id"]
                if chat_id not in groups:
                    groups.append(chat_id)
                    save_groups(groups)
                    print(f"✅ Bot được thêm vào group mới: {chat_id}")

        # --- TRƯỜNG HỢP BOT ĐƯỢC ADD BẰNG new_chat_members ---
        if "message" in update:
            msg = update["message"]
            if "new_chat_members" in msg:
                for mem in msg["new_chat_members"]:
                    if mem["id"] == bot_id:
                        chat_id = msg["chat"]["id"]
                        if chat_id not in groups:
                            groups.append(chat_id)
                            save_groups(groups)
                            print(f"✅ Bot được add vào group: {chat_id}")


# =========================================
# TELEGRAM SEND
# =========================================

def send_telegram(msg):
    groups = load_groups()
    if not groups:
        print("⚠️ Chưa có group nào, bot chưa được add")
        return

    for gid in groups:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": gid,
            "text": msg,
            "disable_web_page_preview": True
        })


# =========================================
# TWEET SCRAPER
# =========================================

def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE))
    return {}


def save_state(state):
    json.dump(state, open(STATE_FILE, "w"))


async def load_cookies(context):
    if not os.path.exists(COOKIES_JSON):
        print("❌ Chưa login — hãy chạy login.py trước")
        return False

    cookies = json.load(open(COOKIES_JSON))
    await context.add_cookies(cookies)
    return True


async def get_latest_tweet(page, username):
    print("Đang kiểm tra tweet người dùng:", username)
    url = f"https://x.com/{username}"
    await page.goto(url, timeout=60000)
    await page.wait_for_timeout(3000)

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

    # --- CHECK BOT ADD GROUP ---
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
                    print("Không đọc được tweet:", username)
                    continue

                tweet_id, text, link = data

                if state.get(username) != tweet_id:
                    send_telegram(f"📢 @{username} vừa đăng tweet:\n\n{text}\n\n{link}")
                    state[username] = tweet_id

            except Exception as e:
                print("Lỗi:", e)

        save_state(state)
        await browser.close()


# =========================================
# MAIN LOOP
# =========================================

if __name__ == "__main__":
    while True:
        check_new_groups()   # kiểm tra bot được add
        asyncio.run(main())  # chạy tweet watcher
        print("✔ done, chờ 60s")
        time.sleep(60)
