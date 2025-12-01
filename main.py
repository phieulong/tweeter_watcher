import asyncio
import json
import os
from playwright.async_api import async_playwright
import requests
import http.server
import threading
import atexit
import sys
import logging

# Configure logging to stdout so Fly captures logs reliably
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

FIRST_TIME = True

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
USERNAMES = ["elonmusk", "nhat1122319"]
# USERNAMES = ["elonmusk", "tommyzz8", "nhat1122319", "BarrySilbert", "metaproph3t", "biancoresearch", "EricBalchunas"]

STATE_FILE = "tweet_state.json"
COOKIES_JSON_1 = "cookies/twitter_cookies_1.json"
COOKIES_JSON_2 = "cookies/twitter_cookies_2.json"
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

async def send_telegram_to_group(gid, msg):
    """Send telegram message to a single group"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(url, json={
            "chat_id": gid,
            "text": msg,
            "disable_web_page_preview": True
        }, timeout=10)
        if response.status_code == 200:
            logging.debug(f"Successfully sent message to group {gid}")
        else:
            logging.warning(f"Failed to send message to group {gid}: {response.status_code}")
    except Exception as e:
        logging.error(f"Error sending message to group {gid}: {e}")

def send_telegram(msg):
    """Send telegram message to all groups in parallel (fire and forget)"""
    groups = load_groups()
    if not groups:
        logging.warning("⚠️ Chưa có group nào, bot chưa được add")
        return

    # Create background tasks for all groups - don't await them
    async def _send_to_all_groups():
        tasks = []
        for gid in groups:
            task = asyncio.create_task(send_telegram_to_group(gid, msg))
            tasks.append(task)

        # Wait for all sends to complete
        await asyncio.gather(*tasks, return_exceptions=True)
        logging.debug(f"Completed sending message to {len(groups)} groups")

    # Start the background task without waiting for it
    try:
        # Get the current event loop
        loop = asyncio.get_event_loop()
        # Schedule the coroutine to run in the background
        loop.create_task(_send_to_all_groups())
    except RuntimeError:
        # If no event loop is running, log the error
        logging.error("No event loop running - cannot send telegram messages")


# =========================================
# TWEET SCRAPER
# =========================================

def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE))
    return {}


def save_state(state):
    json.dump(state, open(STATE_FILE, "w"))


async def load_cookies(context, cookies_address):
    print(f"Loading cookies from {cookies_address}")
    if not os.path.exists(cookies_address):
        logging.error("❌ Chưa login — hãy chạy login.py trước")
        return False

    cookies = json.load(open(cookies_address))
    await context.add_cookies(cookies)
    return True


async def get_latest_tweet(page, username):
    logging.info("Đang kiểm tra tweet người dùng: %s", username)
    url = f"https://x.com/{username}"
    await page.goto(url, timeout=60000)
    await page.wait_for_timeout(60000)
    logging.info("Đã load xong page %s", url)
    if await page.query_selector("div[data-testid='emptyState']"):
        logging.info(f"Page {username} không có state... with {page}", )
        return None
    tweet = await page.query_selector("a[href*='/status/']")
    if not tweet:
        logging.info(f"Page {username} không có status... with {page}", )
    link = await tweet.get_attribute("href")
    tweet_id = link.split("/")[-1]

    text_el = await page.query_selector("div[data-testid='tweetText']")
    text = await text_el.inner_text() if text_el else ""
    if text == "":
        return None
    return tweet_id, text, f"https://x.com/{username}/status/{tweet_id}"


# Rename the original `main` that runs one iteration to `run_once`
async def run_once(page, username):
    """Check tweets for a single username and return the result"""
    logging.info("Bắt đầu kiểm tra tweet mới cho %s...", username)
    data = await get_latest_tweet(page, username)
    if not data:
        logging.info("Không đọc được tweet: %s", username)
        return username, None

    checksum, text, link = data
    return username, (checksum, text, link)


async def _main_loop():
    global FIRST_TIME
    iteration_count = 0

    try:
        while True:
            try:
                # Load state once per iteration
                state = load_state()

                # Determine which cookies file to use based on iteration count
                # Use cookies_1 for iterations 0-1, 4-5, 8-9, etc.
                # Use cookies_2 for iterations 2-3, 6-7, 10-11, etc.
                if (iteration_count // 2) % 2 == 0:
                    cookies_file = COOKIES_JSON_1
                    logging.info("Using cookies file 1 (iteration %d)", iteration_count)
                else:
                    cookies_file = COOKIES_JSON_2
                    logging.info("Using cookies file 2 (iteration %d)", iteration_count)

                # Create tasks for all usernames to run in parallel
                tasks = []
                async with async_playwright() as pw:
                    browser = None
                    try:
                        browser = await pw.chromium.launch()
                        context = await browser.new_context()

                        ok = await load_cookies(context, cookies_file)
                        if not ok:
                            logging.error("Failed to load cookies from %s", cookies_file)
                            iteration_count += 1
                            await asyncio.sleep(3)
                            continue

                        for username in USERNAMES:
                            page = await context.new_page()
                            task = asyncio.create_task(run_once(page, username))
                            tasks.append(task)

                        # Wait for all tasks to complete
                        results = await asyncio.gather(*tasks, return_exceptions=True)

                        # Process results and update state
                        state_updated = False
                        for result in results:
                            if isinstance(result, Exception):
                                logging.exception("Task failed with exception: %s", result)
                                continue

                            username, data = result
                            if data is None:
                                continue

                            tweet_id, text, link = data

                            if state.get(username) != tweet_id:
                                if not FIRST_TIME:
                                    logging.info("User %s vừa mới đăng tweet: %s", username, text[:100] + "..." if len(text) > 100 else text)
                                    # send_telegram(f"📢 @{username} vừa đăng tweet:\n\n{text}\n\n{link}")
                                state[username] = tweet_id
                                state_updated = True

                        # Save state only if there were updates
                        if state_updated:
                            save_state(state)
                        FIRST_TIME = False

                    finally:
                        try:
                            if browser:
                                await page.close()
                                await browser.close()
                        except Exception as _e:
                            logging.exception("Page failed with exception: %s", _e)
                            pass

                # Increment iteration counter after successful iteration
                iteration_count += 1

            except Exception as main_e:
                logging.exception("Error during parallel run_once: %s", main_e)

            logging.info("✔ done, waiting 3s before next check")

            # Wait 3 seconds before next iteration
            await asyncio.sleep(3)

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
