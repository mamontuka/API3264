# Copyright (C) 2026
#
# Authors:
#
# Production-grade version by Oleh Mamont - https://github.com/mamontuka
#
# Based on:
# y13sint - https://github.com/y13sint
# raz0r-code - https://github.com/raz0r-code
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/>.

"""
MODULE: AUTH BROWSER
Playwright-based interactive login.
"""
import os
import json
import time
import asyncio
import logging
from datetime import datetime
from typing import Optional

from playwright.async_api import async_playwright

from config import Config
from .tokens import load_tokens, save_tokens

logger = logging.getLogger(__name__)


async def login_interactive(email=None, password=None, headless=False, clear_existing=False):
    """
    Interactive browser login to obtain Qwen authentication token.
    Uses Playwright to automate browser login to Qwen Chat, then extracts:
    - Authentication token from localStorage
    - Session cookies for request persistence
    This is typically run once during setup, not during normal operation.
    Args:
        email: Optional email for auto-fill login
        password: Optional password for auto-fill login
        headless: Whether to run browser without GUI (default: False for interactive)
        clear_existing: If True, removes all existing tokens before saving new one (default: False)
    Side effects:
        - Launches Chromium browser with persistent user data directory
        - Navigates to Qwen auth page
        - Optionally auto-fills credentials
        - Waits for user to complete login manually
        - Extracts and saves token + cookies to Config.TOKENS_FILE
        - If clear_existing=True, deletes all previous tokens
    """
    # 🔍 DEBUG: Function entry
    logger.debug(f"🔧 login_interactive() called | email={email if email else 'None'}, password={'***' if password else 'None'}, headless={headless}, clear_existing={clear_existing}")

    logger.info("Starting browser for auth (headless=%s)...", headless)

    # Check and create user data directory
    if not os.path.exists(Config.CHROME_USER_DATA):
        logger.debug(f"📁 Creating Chrome user data directory: {Config.CHROME_USER_DATA}")
        os.makedirs(Config.CHROME_USER_DATA, exist_ok=True)
    else:
        logger.debug(f"📁 Chrome user data directory exists: {Config.CHROME_USER_DATA}")

    logger.info(f"Using browser profile: {Config.CHROME_USER_DATA}")

    async with async_playwright() as p:
        logger.debug(f"🌐 Playwright context entered")

        # 🔥 Browser config from Config (no hardcoded values!)
        browser_config = {
            "user_data_dir": Config.CHROME_USER_DATA,
            "headless": headless,
            "viewport": {
                "width": Config.CHROME_VIEWPORT_WIDTH,
                "height": Config.CHROME_VIEWPORT_HEIGHT
            },
            "executable_path": Config.CHROMIUM_EXECUTABLE_PATH,
            "args": Config.CHROMIUM_ARGS,
            "env": os.environ.copy(),
            "ignore_default_args": Config.CHROMIUM_IGNORE_DEFAULT_ARGS
        }

        # Logging the config
        logger.debug(f"🔧 Browser config: {json.dumps({k: v for k, v in browser_config.items() if k != 'env'}, ensure_ascii=False)}")

        try:
            logger.info(f"🚀 Launching Chromium browser...")

            # 🔥 Using **browser_config unpacking
            browser = await p.chromium.launch_persistent_context(**browser_config)

            logger.info(f"✅ Browser launched successfully")
            logger.debug(f"🌐 Browser context created")

        except Exception as e:
            logger.error(f"❌ Failed to launch browser: {type(e).__name__}: {e}")
            logger.debug(f"💡 Possible causes:")
            logger.debug(f"💡   - Chromium not installed at {Config.CHROMIUM_EXECUTABLE_PATH}")
            logger.debug(f"💡   - Missing dependencies (libnss3, libatk1.0, etc.)")
            logger.debug(f"💡   - DISPLAY not set for headless=False")
            logger.debug(f"💡   - Permission denied")
            logger.debug(f"💡   - Invalid args in CHROMIUM_ARGS")
            raise

        try:
            logger.debug(f"📄 Creating new page...")
            page = await browser.new_page()
            logger.debug(f"✅ Page created")

            auth_url = f"{Config.QWEN_BASE_URL}/auth?action=signin"
            logger.info(f"🌐 Navigating to auth URL: {auth_url}")
            logger.debug(f"📤 Going to: {auth_url}")

            try:
                await page.goto(auth_url)
                logger.info(f"✅ Navigation completed")
                logger.debug(f"📄 Current URL: {page.url}")
                logger.debug(f"📄 Page title: {await page.title()}")

            except Exception as nav_err:
                logger.error(f"❌ Navigation failed: {nav_err}")
                logger.debug(f"💡 Check network connectivity and QWEN_BASE_URL config")
                await browser.close()
                return

            # Attempt auto-fill login if credentials provided
            if email and password:
                logger.info(f"🔐 Attempting auto-fill login...")
                logger.debug(f"🔐 Email: {email}")
                logger.debug(f"🔐 Password: {'*' * len(password)}")

                try:
                    # Wait for login form and fill email/username field
                    logger.debug(f"⏳ Waiting for username selector...")
                    await page.wait_for_selector('input[type="text"], input[type="email"], #username', timeout=15000)
                    logger.debug(f"✅ Username selector found")

                    logger.debug(f"✍️ Filling email field...")
                    await page.fill('input[type="text"], input[type="email"], #username', email)
                    logger.debug(f"✅ Email filled")

                    logger.debug(f"⌨️ Pressing Enter...")
                    await page.keyboard.press("Enter")
                    logger.debug(f"✅ Enter pressed")

                    logger.debug(f"⏳ Waiting 3s for transition to password field...")
                    await asyncio.sleep(3)  # Wait for transition to password field
                    logger.debug(f"✅ Wait completed")

                    # Fill password field
                    logger.debug(f"⏳ Waiting for password selector...")
                    await page.wait_for_selector('input[type="password"], #password', timeout=10000)
                    logger.debug(f"✅ Password selector found")

                    logger.debug(f"✍️ Filling password field...")
                    await page.fill('input[type="password"], #password', password)
                    logger.debug(f"✅ Password filled")

                    logger.debug(f"⌨️ Pressing Enter...")
                    await page.keyboard.press("Enter")
                    logger.debug(f"✅ Enter pressed")

                    logger.info(f"✅ Auto-fill completed successfully")
                    logger.debug(f"💡 Waiting for login to complete...")

                except Exception as e:
                    logger.warning(f"⚠️ Auto-fill failed: {type(e).__name__}: {e}")
                    logger.debug(f"💡 Possible causes:")
                    logger.debug(f"💡   - Selectors changed on Qwen auth page")
                    logger.debug(f"💡   - Page loaded too slowly (timeout)")
                    logger.debug(f"💡   - 2FA required")
                    logger.debug(f"💡 Continuing with manual login...")
            else:
                logger.debug(f"ℹ️ No credentials provided, skipping auto-fill")

            # Prompt user to complete login manually in browser
            print("\n" + "="*50 + "\n               AUTHORIZATION\n" + "="*50)
            print("1. Login to Qwen account in browser.\n2. Wait for chat interface.\n3. Press Enter here.")
            print("="*50 + "\n")

            logger.info(f"⏳ Waiting for user to complete login...")
            logger.debug(f"💡 User should login in browser and press Enter in terminal")

            # Use asyncio.to_thread for non-blocking input in async context
            try:
                await asyncio.to_thread(lambda: input("Press Enter after successful login..."))
                logger.info(f"✅ User confirmed login completion")
            except Exception as input_err:
                logger.error(f"❌ Input error: {input_err}")
                await browser.close()
                return

            # Wait a bit for page to stabilize after login
            logger.debug(f"⏳ Waiting 2s for page to stabilize...")
            await asyncio.sleep(2)
            logger.debug(f"📄 Current URL after login: {page.url}")
            logger.debug(f"📄 Page title after login: {await page.title()}")

            # Extract authentication token from browser localStorage
            logger.info(f"🔑 Extracting authentication token...")
            token = None

            try:
                # Debug: List all localStorage keys first
                logger.debug(f"🔍 Checking localStorage keys...")
                storage_keys = await page.evaluate("Object.keys(localStorage)")
                logger.debug(f"📦 localStorage keys: {storage_keys}")

                # Check for common token key names
                possible_keys = ['token', 'auth_token', 'accessToken', 'access_token', 'qwen_token']
                for key in possible_keys:
                    if key in storage_keys:
                        logger.debug(f"✅ Found potential token key: '{key}'")

                if 'token' not in storage_keys:
                    logger.warning(f"⚠️ 'token' key not found in localStorage!")
                    logger.debug(f"💡 Available keys: {storage_keys}")
                    logger.debug(f"💡 Qwen may have changed the token key name")

                # Extract token
                logger.debug(f"🔑 Extracting token from localStorage.getItem('token')...")
                token = await page.evaluate("localStorage.getItem('token')")

                if token:
                    token_preview = token[:8] + '...' if len(token) > 8 else token
                    logger.info(f"✅ Token extracted successfully: {token_preview}")
                    logger.debug(f"🔑 Token length: {len(token)} chars")
                else:
                    logger.error(f"❌ Token is null/empty in localStorage")
                    logger.debug(f"💡 Possible causes:")
                    logger.debug(f"💡   - Login not completed successfully")
                    logger.debug(f"💡   - Token stored under different key")
                    logger.debug(f"💡   - Token stored in cookies only")
                    logger.debug(f"💡   - Qwen changed auth mechanism")

                    # Try alternative extraction methods
                    logger.debug(f"🔍 Trying alternative token extraction...")

                    # Check cookies for token
                    logger.debug(f"🍪 Checking cookies for token...")
                    cookies_alt = await page.context.cookies()
                    for cookie in cookies_alt:
                        if 'token' in cookie.get('name', '').lower():
                            logger.debug(f"🍪 Found token-like cookie: {cookie['name']}")

            except Exception as e:
                logger.error(f"❌ Failed to get token: {type(e).__name__}: {e}")
                logger.debug(f"📋 Full exception:", exc_info=True)
                await browser.close()
                return

            if not token:
                logger.error("❌ Token not found! Cannot proceed.")
                logger.debug(f"💡 Suggestions:")
                logger.debug(f"💡   - Ensure you logged in successfully in browser")
                logger.debug(f"💡   - Wait for chat interface to fully load before pressing Enter")
                logger.debug(f"💡   - Check if Qwen changed their auth mechanism")
                await browser.close()
                return

            # Extract session cookies for request persistence
            logger.info(f"🍪 Extracting session cookies...")
            try:
                cookies = await page.context.cookies()
                logger.info(f"✅ Extracted {len(cookies)} cookies")

                # Log cookie details
                for idx, cookie in enumerate(cookies):
                    cookie_name = cookie.get('name', 'unknown')
                    cookie_domain = cookie.get('domain', 'unknown')
                    cookie_value_preview = cookie.get('value', '')[:8] + '...' if len(cookie.get('value', '')) > 8 else cookie.get('value', '')
                    logger.debug(f"🍪 Cookie[{idx}]: name={cookie_name}, domain={cookie_domain}, value={cookie_value_preview}")

                # Check for important cookies
                important_cookies = ['token', 'session', 'auth', 'qwen']
                found_important = []
                for cookie in cookies:
                    for imp in important_cookies:
                        if imp in cookie.get('name', '').lower():
                            found_important.append(cookie['name'])

                if found_important:
                    logger.debug(f"✅ Found important cookies: {found_important}")
                else:
                    logger.warning(f"⚠️ No obvious auth cookies found")

            except Exception as e:
                logger.error(f"❌ Failed to extract cookies: {e}")
                cookies = []

            # 🔥 IMPROVEMENT: Load or clear existing tokens
            logger.debug(f"💾 Preparing token data for save...")
            if clear_existing:
                logger.info("🧹 Clearing existing tokens (clear_existing=True)...")
                tokens = []
                logger.debug(f"🗑️ All previous tokens removed from memory")
            else:
                tokens = load_tokens()
                logger.debug(f"📦 Loaded {len(tokens)} existing tokens from file")

            account_name = email or f"acc_{int(time.time() * 1000)}"
            logger.debug(f"👤 Account name: {account_name}")

            # Remove existing entry for this account to avoid duplicates
            old_count = len(tokens)
            tokens = [t for t in tokens if t['id'] != account_name]
            new_count = len(tokens)

            if old_count != new_count:
                logger.debug(f"🗑️ Removed existing token for account: {account_name}")
            else:
                logger.debug(f"➕ No existing token for account, adding new")

            # Add new token entry
            token_entry = {
                "id": account_name,
                "token": token,
                "cookies": cookies,
                "added_at": datetime.now().isoformat(),
                "invalid": False,
                "resetAt": None
            }
            tokens.append(token_entry)

            logger.debug(f"📦 Token entry prepared:")
            logger.debug(f"   id: {token_entry['id']}")
            logger.debug(f"   token: {token[:8]}...")
            logger.debug(f"   cookies_count: {len(token_entry['cookies'])}")
            logger.debug(f"   added_at: {token_entry['added_at']}")
            logger.debug(f"   invalid: {token_entry['invalid']}")
            logger.debug(f"   resetAt: {token_entry['resetAt']}")

            # Save tokens
            logger.info(f"💾 Saving tokens to {Config.TOKENS_FILE}...")
            try:
                save_tokens(tokens)
                logger.info(f"✅ Tokens saved successfully")
                logger.debug(f"📁 File: {Config.TOKENS_FILE}")
                logger.debug(f"📊 Total tokens in file: {len(tokens)}")
            except Exception as save_err:
                logger.error(f"❌ Failed to save tokens: {save_err}")
                await browser.close()
                return

            logger.info(f"✅ Account {account_name} added successfully!")
            logger.debug(f"🎉 Login process completed successfully")

        except Exception as e:
            logger.error(f"❌ Unexpected error during login: {type(e).__name__}: {e}")
            logger.debug(f"📋 Full exception:", exc_info=True)
            raise

        finally:
            logger.debug(f"🔒 Closing browser...")
            await browser.close()
            logger.debug(f"✅ Browser closed")
