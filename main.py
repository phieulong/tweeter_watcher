import asyncio
import atexit
import http.server
import json
import logging
import os
import sys
import threading
from typing import Dict, List, Optional, Tuple, Any

import requests
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

# Configure logging to stdout so Fly captures logs reliably
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# Constants
BOT_TOKEN = "8142781290:AAFM6d0H4Cv4f1YIZkvbQAOON1shB0L0QHg"
USERNAMES = ["elonmusk", "nhat1122319"]
STATE_FILE = "tweet_state.json"
COOKIES_JSON_1 = "cookies/twitter_cookies_1.json"
COOKIES_JSON_2 = "cookies/twitter_cookies_2.json"
GROUP_FILE = "groups.json"
OFFSET_FILE = "update_offset.json"
SLEEP_INTERVAL = 30
PAGE_LOAD_TIMEOUT = 60000
WAIT_PAGE_TO_LOAD = 10000

# Global state
_first_time = True
_health_server = None

class HealthCheckHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for health check endpoint"""

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_success_response()
        else:
            self._send_not_found_response()

    def _send_success_response(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def _send_not_found_response(self) -> None:
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        """Silence logs from http.server"""
        return


class HealthServer:
    """Manages the health check server for monitoring"""

    def __init__(self, port: int = None):
        self.port = port or int(os.environ.get("PORT", "8080"))
        self.server = None
        self.thread = None

    def start(self) -> bool:
        """Start the health server in a daemon thread"""
        try:
            http.server.ThreadingHTTPServer.allow_reuse_address = True
            self.server = http.server.ThreadingHTTPServer(
                ("0.0.0.0", self.port), HealthCheckHandler
            )

            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()

            logging.info(f"Health server running on 0.0.0.0:{self.port}/healthz")
            atexit.register(self.cleanup)
            return True

        except OSError as e:
            logging.error(f"Failed to start health server on port {self.port}: {e}")
            return False

    def cleanup(self) -> None:
        """Clean shutdown of the server"""
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass



class GroupManager:
    """Manages Telegram groups for the bot"""

    @staticmethod
    def load_groups() -> List[int]:
        """Load list of group IDs from file"""
        if not os.path.exists(GROUP_FILE):
            return []
        with open(GROUP_FILE, 'r') as f:
            return json.load(f)

    @staticmethod
    def save_groups(groups: List[int]) -> None:
        """Save list of group IDs to file"""
        with open(GROUP_FILE, 'w') as f:
            json.dump(groups, f)

    @staticmethod
    def load_offset() -> int:
        """Load update offset from file"""
        if not os.path.exists(OFFSET_FILE):
            return 0
        with open(OFFSET_FILE, 'r') as f:
            return json.load(f)

    @staticmethod
    def save_offset(offset: int) -> None:
        """Save update offset to file"""
        with open(OFFSET_FILE, 'w') as f:
            json.dump(offset, f)

    def check_new_groups(self) -> None:
        """Check for new groups using Telegram getUpdates API"""
        offset = self.load_offset()
        logging.info("Checking for new groups with offset %s", offset)

        try:
            response = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 0}
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            logging.error("Failed to get updates: %s", e)
            return

        if not data.get("ok"):
            logging.warning("Telegram API returned error: %s", data)
            return

        groups = self.load_groups()
        bot_id = int(BOT_TOKEN.split(":")[0])

        for update in data["result"]:
            update_id = update["update_id"]
            self.save_offset(update_id + 1)

            # Handle bot added via my_chat_member
            if "my_chat_member" in update:
                self._handle_chat_member_update(update["my_chat_member"], groups)

            # Handle bot added via new_chat_members
            if "message" in update:
                self._handle_new_chat_members(update["message"], groups, bot_id)

    def _handle_chat_member_update(self, chat_member_update: Dict, groups: List[int]) -> None:
        """Handle my_chat_member updates"""
        chat = chat_member_update["chat"]
        old_status = chat_member_update["old_chat_member"]["status"]
        new_status = chat_member_update["new_chat_member"]["status"]

        if old_status == "left" and new_status in ["member", "administrator"]:
            chat_id = chat["id"]
            if chat_id not in groups:
                groups.append(chat_id)
                self.save_groups(groups)
                logging.info("✅ Bot added to new group via chat member update: %s", chat_id)

    def _handle_new_chat_members(self, message: Dict, groups: List[int], bot_id: int) -> None:
        """Handle new_chat_members updates"""
        if "new_chat_members" not in message:
            return

        for member in message["new_chat_members"]:
            if member["id"] == bot_id:
                chat_id = message["chat"]["id"]
                if chat_id not in groups:
                    groups.append(chat_id)
                    self.save_groups(groups)
                    logging.info("✅ Bot added to group via new chat members: %s", chat_id)


class TelegramNotifier:
    """Handles sending notifications to Telegram groups"""

    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self.group_manager = GroupManager()

    async def send_to_group(self, group_id: int, message: str) -> bool:
        """Send message to a single Telegram group"""
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        try:
            response = requests.post(
                url,
                json={
                    "chat_id": group_id,
                    "text": message,
                    "disable_web_page_preview": True
                },
                timeout=10
            )

            if response.status_code == 200:
                logging.debug(f"Successfully sent message to group {group_id}")
                return True
            else:
                logging.warning(f"Failed to send message to group {group_id}: {response.status_code}")
                return False

        except Exception as e:
            logging.error(f"Error sending message to group {group_id}: {e}")
            return False

    def send_to_all_groups(self, message: str) -> None:
        """Send message to all groups (fire and forget)"""
        groups = self.group_manager.load_groups()

        if not groups:
            logging.warning("⚠️ No groups found, bot has not been added to any groups")
            return

        async def _send_to_all():
            tasks = [
                asyncio.create_task(self.send_to_group(group_id, message))
                for group_id in groups
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)
            success_count = sum(1 for result in results if result is True)
            logging.info(f"Message sent to {success_count}/{len(groups)} groups")

        try:
            loop = asyncio.get_event_loop()
            loop.create_task(_send_to_all())
        except RuntimeError:
            logging.error("No event loop running - cannot send telegram messages")


class TwitterScraper:
    """Handles Twitter scraping operations"""

    def __init__(self, state_file: str):
        self.state_file = state_file

    def load_state(self) -> Dict[str, str]:
        """Load tweet state from file"""
        if os.path.exists(self.state_file):
            with open(self.state_file, 'r') as f:
                return json.load(f)
        return {}

    def save_state(self, state: Dict[str, str]) -> None:
        """Save tweet state to file"""
        with open(self.state_file, 'w') as f:
            json.dump(state, f)

    async def load_cookies(self, context: BrowserContext, cookies_file: str) -> bool:
        """Load cookies from file into browser context"""
        logging.info(f"Loading cookies from {cookies_file}")

        if not os.path.exists(cookies_file):
            logging.error("❌ Cookies file not found — please run login.py first")
            return False

        try:
            with open(cookies_file, 'r') as f:
                cookies = json.load(f)
            await context.add_cookies(cookies)
            return True
        except Exception as e:
            logging.error(f"Failed to load cookies from {cookies_file}: {e}")
            return False

    async def get_latest_tweet(self, page: Page, username: str) -> Optional[Tuple[str, str, str]]:
        """Get the latest tweet for a username"""
        logging.info("Checking tweets for user: %s", username)

        try:
            url = f"https://x.com/{username}"
            await page.goto(url, timeout=PAGE_LOAD_TIMEOUT)
            await page.wait_for_timeout(WAIT_PAGE_TO_LOAD)  # Wait for page to load

            logging.info("Page loaded for %s", url)

            # Check if page is empty
            if await page.query_selector("div[data-testid='emptyState']"):
                logging.info(f"Page {username} has no tweets")
                return None

            # Find all tweet links (filter out analytics, photo, video links)
            all_links = await page.query_selector_all("a[href*='/status/']")
            tweet_links = []

            for link in all_links:
                href = await link.get_attribute("href")
                # Filter out analytics, photo, video, and other non-tweet links
                if href and not any(keyword in href for keyword in ['/analytics', '/photo/', '/video/', '/retweets', '/quotes', '/likes']):
                    # Ensure it's a direct tweet link (ends with status/ID or status/ID/)
                    parts = href.split('/')
                    if 'status' in parts:
                        status_index = parts.index('status')
                        if status_index + 1 < len(parts) and parts[status_index + 1].isdigit():
                            tweet_links.append(link)

            if not tweet_links:
                logging.info(f"No tweets found for {username}")
                return None

            # Check the first tweet to see if it's pinned
            first_tweet_link = tweet_links[0]
            first_link = await first_tweet_link.get_attribute("href")
            first_tweet_id = first_link.split("/")[-1].split('?')[0]  # Remove query params if any

            # Check if the first tweet is pinned
            is_pinned = False
            pin_indicators = [
                "div[data-testid='socialContext']",  # General social context container
                "svg[aria-label*='Pin']",  # Pin icon
                "svg[data-testid='pin']",  # Pin test id
                "*:has-text('Pinned Tweet')",  # Text indicator
                "*:has-text('Pinned')",  # Shorter pin text
                "span:has-text('📌')",  # Pin emoji
            ]

            for indicator in pin_indicators:
                try:
                    pin_element = await page.query_selector(indicator)
                    if pin_element:
                        pin_text = await pin_element.inner_text()
                        if any(keyword in pin_text.lower() for keyword in ['pin', 'pinned', '📌']):
                            is_pinned = True
                            break
                except Exception:
                    continue

            # Determine which tweet to use
            if is_pinned and len(tweet_links) > 1:
                # First tweet is pinned, use the second tweet (latest actual tweet)
                tweet_link = tweet_links[1]
                link = await tweet_link.get_attribute("href")
                tweet_id = link.split("/")[-1].split('?')[0]  # Remove query params if any

                # Log information about both tweets
                logging.info(f"First tweet {first_tweet_id} for @{username}: 📌 PINNED")
                logging.info(f"Using next tweet {tweet_id} for @{username}: 🔹 LATEST ACTUAL TWEET")
            else:
                # Use the first tweet (either not pinned or only one tweet available)
                tweet_link = first_tweet_link
                link = first_link
                tweet_id = first_tweet_id

                pin_status = "📌 PINNED" if is_pinned else "🔹 NOT PINNED"
                logging.info(f"Tweet {tweet_id} for @{username}: {pin_status}")

            # Get tweet text from the selected tweet
            # Navigate to the tweet's container to find the text
            text_element = None
            try:
                # Try to find the text element near the selected tweet link
                tweet_articles = await page.query_selector_all("article[data-testid='tweet']")
                for article in tweet_articles:
                    article_link = await article.query_selector("a[href*='/status/']")
                    if article_link:
                        article_href = await article_link.get_attribute("href")
                        if tweet_id in article_href:
                            text_element = await article.query_selector("div[data-testid='tweetText']")
                            break

                # Fallback: try to find any tweet text element
                if not text_element:
                    text_element = await page.query_selector("div[data-testid='tweetText']")

            except Exception as e:
                logging.warning(f"Error finding tweet text for {tweet_id}: {e}")
                text_element = await page.query_selector("div[data-testid='tweetText']")

            text = await text_element.inner_text() if text_element else ""

            if not text:
                logging.info(f"No text found for tweet {tweet_id}")
                return None


            full_link = f"https://x.com/{username}/status/{tweet_id}"
            return tweet_id, text, full_link

        except Exception as e:
            logging.error(f"Error getting latest tweet for {username}: {e}")
            return None

    async def check_user_tweets(self, page: Page, username: str) -> Tuple[str, Optional[Tuple[str, str, str]]]:
        """Check tweets for a single username and return the result"""
        logging.info("Starting tweet check for %s...", username)

        data = await self.get_latest_tweet(page, username)
        if not data:
            logging.info("Could not read tweets for: %s", username)
            return username, None

        tweet_id, text, link = data
        return username, (tweet_id, text, link)


class TwitterWatcher:
    """Main application class that orchestrates the Twitter watching functionality"""

    def __init__(self):
        self.scraper = TwitterScraper(STATE_FILE)
        self.notifier = TelegramNotifier(BOT_TOKEN)
        self.group_manager = GroupManager()
        self.health_server = None
        self.iteration_count = 0
        self.is_first_run = True

    def start_health_server(self) -> bool:
        """Start the health check server"""
        self.health_server = HealthServer()
        return self.health_server.start()

    def _get_cookies_file(self) -> str:
        """Determine which cookies file to use based on iteration count"""
        # Use cookies_1 for iterations 0-1, 4-5, 8-9, etc.
        # Use cookies_2 for iterations 2-3, 6-7, 10-11, etc.
        if (self.iteration_count // 2) % 2 == 0:
            cookies_file = COOKIES_JSON_1
            logging.info("Using cookies file 1 (iteration %d)", self.iteration_count)
        else:
            cookies_file = COOKIES_JSON_2
            logging.info("Using cookies file 2 (iteration %d)", self.iteration_count)
        return cookies_file

    async def _process_user_batch(self, context: BrowserContext, usernames: List[str]) -> List[Tuple[str, Optional[Tuple[str, str, str]]]]:
        """Process a batch of usernames in parallel"""
        tasks = []

        for username in usernames:
            page = await context.new_page()
            task = asyncio.create_task(self.scraper.check_user_tweets(page, username))
            tasks.append(task)

        # Wait for all tasks to complete
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process and return results
        processed_results = []
        for result in results:
            if isinstance(result, Exception):
                logging.exception("Task failed with exception: %s", result)
                continue
            processed_results.append(result)

        return processed_results

    async def _handle_tweet_updates(self, results: List[Tuple[str, Optional[Tuple[str, str, str]]]]) -> None:
        """Handle tweet updates and notifications"""
        state = self.scraper.load_state()
        state_updated = False

        for username, data in results:
            if data is None:
                continue

            tweet_id, text, link = data

            if state.get(username) != tweet_id:
                if not self.is_first_run:
                    truncated_text = text[:100] + "..." if len(text) > 100 else text
                    logging.info("User %s posted a new tweet: %s", username, truncated_text)

                    # Send notification
                    message = f"📢 @{username} just posted:\n\n{text}\n\n{link}"
                    self.notifier.send_to_all_groups(message)

                state[username] = tweet_id
                state_updated = True

        # Save state only if there were updates
        if state_updated:
            self.scraper.save_state(state)

        self.is_first_run = False

    async def _run_single_iteration(self) -> bool:
        """Run a single iteration of tweet checking"""
        try:
            cookies_file = self._get_cookies_file()

            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                context = await browser.new_context()

                try:
                    # Load cookies
                    if not await self.scraper.load_cookies(context, cookies_file):
                        logging.error("Failed to load cookies from %s", cookies_file)
                        return False

                    # Process all usernames
                    results = await self._process_user_batch(context, USERNAMES)

                    # Handle updates
                    await self._handle_tweet_updates(results)

                    return True

                finally:
                    try:
                        await browser.close()
                    except Exception as e:
                        logging.exception("Error closing browser: %s", e)

        except Exception as e:
            logging.exception("Error during iteration: %s", e)
            return False

    async def run(self) -> None:
        """Main application loop"""
        # Start health server
        if not self.start_health_server():
            logging.warning("Failed to start health server")

        try:
            while True:
                # Check for new groups periodically
                # TODO: uncomment
                # if self.iteration_count % 10 == 0:
                #     self.group_manager.check_new_groups()

                # Run iteration
                success = await self._run_single_iteration()

                if success:
                    self.iteration_count += 1

                logging.info("✔ Iteration completed, waiting %ds before next check", SLEEP_INTERVAL)
                await asyncio.sleep(SLEEP_INTERVAL)

        except KeyboardInterrupt:
            logging.info("Interrupted by user")
        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        """Clean up resources"""
        logging.info("Cleaning up resources and exiting")
        if self.health_server:
            self.health_server.cleanup()


async def main() -> None:
    """Main entry point for the application"""
    watcher = TwitterWatcher()
    await watcher.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Application interrupted by user")
    except Exception as e:
        logging.exception("Application failed with error: %s", e)
