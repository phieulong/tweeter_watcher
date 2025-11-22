import asyncio
import hashlib
import json
import os
from time import sleep

from playwright.async_api import async_playwright
import requests
import http.server
import threading
import atexit
import typing
import sys
import logging

# Configure logging to stdout so Fly captures logs reliably
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


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
        # type: ignore - handler type stub is incompatible with our BaseHTTPRequestHandler subclass
        server = http.server.ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)  # type: ignore[arg-type]
    except OSError as e:
        print(f"Failed to start health server on port {port}: {e}")
        return None

    # serve in a daemon thread
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logging.info(f"Health server running on 0.0.0.0:{port}/healthz")

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
    logging.warning("Warning: cannot start health server: %s", e)

BOT_TOKEN = "8142781290:AAFM6d0H4Cv4f1YIZkvbQAOON1shB0L0QHg"
USERNAMES = ["elonmusk", "tommyzz8", "nhat1122319", "BarrySilbert", "metaproph3t", "biancoresearch", "EricBalchunas"]

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
    logging.info("Kiểm tra bot được add vào group mới với offset %s", offset)
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
        logging.error("❌ Chưa login — hãy chạy login.py trước")
        return False

    cookies = json.load(open(COOKIES_JSON))
    await context.add_cookies(cookies)
    return True


async def get_latest_tweet(page, username):
    logging.info("Đang kiểm tra tweet người dùng: %s", username)
    url = f"https://x.com/{username}"
    await page.goto(url, timeout=10000)
    await page.wait_for_timeout(3000)
    logging.info("Đã load xong page %s", url)
    if await page.query_selector("div[data-testid='emptyState']"):
        logging.info(f"Page {username} không có state... with {page}", )
        return None
    logging.info("Page %s có status... with %s", username, page)
    # tweet = await page.query_selector_all("a[href*='/status/']")
    # if not tweet:
    #     logging.info(f"Page {username} không có status... with {page}", )
    #     return None
    # logging.info("Page %s có status... with %s", username, page)
    # link = await tweet.get_attribute("href")
    # tweet_id = link.split("/")[-1]

    text_el = await page.query_selector("div[data-testid='tweetText']")
    text = await text_el.inner_text() if text_el else ""
    tweet_checksum = hashlib.sha256(text.encode()).hexdigest()

    return tweet_checksum, text, f"https://twitter.com/status/{username}"


# Rename the original `main` that runs one iteration to `run_once`
async def run_once():
    state = load_state()
    logging.info("Bắt đầu kiểm tra tweet mới...")
    # --- CHECK BOT ADD GROUP ---
    # keep this synchronous call off the event loop in the caller if desired
    # check_new_groups()

    async with async_playwright() as pw:
        browser = None
        try:
            # pass recommended args for running Chromium in containerized environments
            browser = await pw.chromium.launch()
            context = await browser.new_context()

            ok = await load_cookies(context)
            if not ok:
                return

            page = await context.new_page()
            # await page.goto("https://x.com/home")

            if "login" in page.url.lower():
                logging.error("❌ Cookie hết hạn — hãy chạy login.py để login lại")
                return

            for username in USERNAMES:
                try:
                    data = await get_latest_tweet(page, username)
                    if not data:
                        print("Không đọc được tweet:", username)
                        continue

                    tweet_id, text, link = data

                    if state.get(username) != tweet_id:
                        logging.info(f"User {tweet_id} vừa mới đăng tweet {text}")
                        send_telegram(f"📢 @{username} vừa đăng tweet:\n\n{text}\n\n{link}")
                        state[username] = tweet_id

                except Exception as e:
                    print("Lỗi:", e)

            save_state(state)
        finally:
            try:
                if browser:
                    await page.close()
                    await browser.close()
            except Exception:
                pass


# New top-level asyncio runner with graceful shutdown
import signal

async def _main_loop():

    try:
        while True:
            # check new groups (blocking) off the loop to avoid blocking
            # await asyncio.to_thread(check_new_groups)

            try:
                await run_once()
            except Exception as e:
                logging.exception("Error during run_once: %s", e)

            logging.info("✔ done")
            sleep(60)

            # wait up to 60 seconds or until shutdown
    finally:
        # Clean up health server on shutdown
        logging.info("Cleaning up health server and exiting")
        try:
            if _health_server:
                _health_server.shutdown()
                _health_server.server_close()
        except Exception:
            pass


# Replace the old blocking main loop with a proper asyncio.run invocation
if __name__ == "__main__":
    try:
        asyncio.run(_main_loop())
    except KeyboardInterrupt:
        logging.info("Interrupted by user")

