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
MODULE: SELENIUM BRIDGE (Playwright-based)
Scheme implementation: Base64 -> Temp File -> Browser Upload -> Response

Purpose:
Provides image uploading to Qwen web chat via browser emulation,
when the direct API does not support multimodal requests or causes conflicts.
"""
import os
import base64
import uuid
import asyncio
import logging
from pathlib import Path
from typing import Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from config import Config

logger = logging.getLogger(__name__)


class SeleniumBridge:
    """Manager for downloading files via browser using Playwright."""

    def __init__(self, page: Page):
        self.page = page
        self.temp_files: list[Path] = []

    async def save_base64_to_temp(self, base64_data: str, prefix: str = "img") -> Path:
        """
        Saves base64 data to a temporary file.

        Args:
            base64_data: Base64 string (with or without data:image/... prefix)
            prefix: File name prefix

        Returns:
            Path: Path to the created temporary file
        """
        try:
            # Parsing base64 with a possible prefix
            header, encoded = base64_data.split(",", 1) if "," in base64_data else ("", base64_data)

            # Determine the extension by MIME type
            ext = ".png"
            if "jpeg" in header or "jpg" in header:
                ext = ".jpg"
            elif "gif" in header:
                ext = ".gif"
            elif "webp" in header:
                ext = ".webp"

            filename = f"{prefix}_{uuid.uuid4().hex}{ext}"
            filepath = Config.TEMP_FILES_DIR / filename

            # Decode and save
            with open(filepath, "wb") as f:
                f.write(base64.b64decode(encoded))

            self.temp_files.append(filepath)
            logger.debug(f"💾 Saved temp file: {filepath}")
            return filepath

        except Exception as e:
            logger.error(f"❌ Failed to save base64: {e}")
            raise

    async def upload_file_and_wait(self, filepath: Path, prompt_text: str = "", chat_id: str = None, target_model: str = None) -> dict:
        """
        Uploads a file by pasting image from clipboard via JavaScript emulation.
        Uses non-blocking polling with hard global timeout to prevent hangs.
        Returns: {"text": str, "image_url": str | None}
        """
        try:
            # 0. Go to the chat URL if chat_id is passed
            if chat_id:
                chat_url = f"https://chat.qwen.ai/c/{chat_id}"
                logger.debug(f"🌐 Navigating to chat: {chat_url}")
                await self.page.goto(chat_url, wait_until="domcontentloaded", timeout=30000)
                await self.page.locator("textarea").first.wait_for(state="visible", timeout=15000)
                await asyncio.sleep(1.0)
                logger.debug("✅ Chat page loaded and ready")

                # 🔥 SWITCH THE MODEL IN THE UI IF SPECIFIED
                if target_model:
                    logger.debug(f"🎯 Switching model to: {target_model}")
                    try:
                        model_selector = self.page.locator(f"button:has-text('{target_model}'), .model-selector button, [class*='model'] button").first
                        await model_selector.wait_for(state="visible", timeout=5000)
                        await model_selector.click()
                        await asyncio.sleep(1.0)
                        logger.debug(f"✅ Model switched to {target_model}")
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to switch model to {target_model}: {e}")

            # 🔥 Count existing images BEFORE sending
            existing_images = await self.page.locator("img[src*='cdn.qwenlm.ai']").count()
            logger.debug(f"📊 Existing images before upload: {existing_images}")

            # 1. Read the file and convert it to base64
            with open(filepath, "rb") as f:
                file_data = f.read()
                base64_data = base64.b64encode(file_data).decode('utf-8')

            ext = filepath.suffix.lower()
            mime_type = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp'}.get(ext, 'image/png')
            logger.debug(f"📷 Preparing image for paste: {filepath.name} ({mime_type})")

            # 2. Find the textarea and focus on it
            textarea = self.page.locator("textarea").first
            await textarea.wait_for(state="visible", timeout=5000)
            await textarea.click()
            logger.debug("🎯 Textarea focused")

            # 3. Insert the request text (if any)
            if prompt_text:
                await textarea.fill(prompt_text)
                logger.debug(f"✍️ Prompt entered ({len(prompt_text)} chars)")

            # 4. Simulating a paste event with an image using JavaScript
            paste_script = """
            async (args) => {
                const { base64Data, mimeType } = args;
                const byteCharacters = atob(base64Data);
                const byteNumbers = new Array(byteCharacters.length);
                for (let i = 0; i < byteCharacters.length; i++) { byteNumbers[i] = byteCharacters.charCodeAt(i); }
                const byteArray = new Uint8Array(byteNumbers);
                const blob = new Blob([byteArray], { type: mimeType });
                const file = new File([blob], 'image.png', { type: mimeType });
                const dataTransfer = new DataTransfer();
                dataTransfer.items.add(file);
                const pasteEvent = new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: dataTransfer });
                const activeElement = document.activeElement;
                if (activeElement) { activeElement.dispatchEvent(pasteEvent); return true; }
                return false;
            }
            """
            result = await self.page.evaluate(paste_script, {"base64Data": base64_data, "mimeType": mime_type})
            if result:
                logger.debug("✅ Paste event dispatched successfully")
            else:
                logger.warning("⚠️ Paste event dispatch returned false")

            await asyncio.sleep(1.5)

            # 🔥 Using selectors from the config
            preview_found = False
            for selector in Config.QWEN_PREVIEW_SELECTORS:
                try:
                    await self.page.wait_for_selector(selector, state="visible", timeout=3000)
                    logger.debug(f"✅ Image preview detected: {selector}")
                    preview_found = True
                    break
                except: continue
            if not preview_found:
                logger.warning("⚠️ Preview not detected, but proceeding...")

            # 6. Sending a message
            send_btn = self.page.locator(Config.QWEN_SEND_BUTTON_SELECTOR).last
            await send_btn.wait_for(state="visible", timeout=3000)
            await send_btn.click()
            logger.debug("📤 Send button clicked")

            # 7. 🔥 Waiting for the model's response - RELIABLE POLLING WITH GLOBAL TIMEOUT
            timeout_sec = Config.BROWSER_ACTION_TIMEOUT
            deadline = asyncio.get_event_loop().time() + timeout_sec
            check_interval = 0.5
            last_log_time = asyncio.get_event_loop().time()
            logger.info(f"⏳ Waiting for response (hard_timeout={timeout_sec}s)...")

            result_text = ""
            new_image_url = None
            response_found = False

            while asyncio.get_event_loop().time() < deadline:
                current_time = asyncio.get_event_loop().time()
                if current_time - last_log_time >= 5.0:
                    elapsed = current_time - (deadline - timeout_sec)
                    logger.debug(f"⏳ Still waiting... ({elapsed:.1f}s / {timeout_sec}s)")
                    last_log_time = current_time

                # 🔥 CHECK FOR NEW IMAGE
                current_images = await self.page.locator("img[src*='cdn.qwenlm.ai']").count()
                if current_images > existing_images and not new_image_url:
                    logger.debug(f"✅ New image detected! ({existing_images} → {current_images})")
                    await asyncio.sleep(2.0)
                    latest_img = self.page.locator("img[src*='cdn.qwenlm.ai']").last
                    img_url = await latest_img.get_attribute("src")
                    if img_url:
                        if "&x-oss-process=" in img_url:
                            img_url = img_url.split("&x-oss-process=", 1)[0]
                        new_image_url = img_url
                        logger.info(f"🖼️ Extracted CDN image URL: {img_url[:80]}...")

                # Checking selectors in a non-blocking way
                for selector in Config.QWEN_RESPONSE_SELECTORS:
                    try:
                        locator = self.page.locator(selector).last
                        if await locator.is_visible():
                            text = await locator.inner_text()
                            if text and len(text.strip()) > 5:
                                logger.debug(f"✅ Response detected via selector: {selector}")
                                logger.debug(f"⏳ Waiting for generation to complete...")
                                await asyncio.sleep(3.0)
                                prev_text = text
                                for _ in range(3):
                                    await asyncio.sleep(1.0)
                                    current_text = await locator.inner_text()
                                    if current_text == prev_text: break
                                    prev_text = current_text
                                result_text = prev_text
                                logger.debug(f"✅ Generation complete! ({len(result_text)} chars)")
                                response_found = True
                                break
                    except Exception: continue

                if response_found: break

                try:
                    last_msg = self.page.locator("div[class*='message']").last
                    if await last_msg.is_visible():
                        text = await last_msg.inner_text()
                        if text and len(text.strip()) > 10:
                            logger.debug(f"✅ Response detected via fallback generic selector")
                            await asyncio.sleep(3.0)
                            result_text = await last_msg.inner_text()
                            response_found = True
                            break
                except Exception: pass

                await asyncio.sleep(check_interval)

            if not response_found and not new_image_url:
                elapsed = timeout_sec
                logger.error(f"❌ Response timeout after {elapsed:.1f}s")
                raise TimeoutError(f"Vision response timeout after {timeout_sec}s. No valid response detected.")

            if result_text:
                await asyncio.sleep(2.0)
                logger.info(f"✅ Received response ({len(result_text)} chars)")

            # 🔥 Returning the dictionary: text + CDN link (if available)
            return {"text": result_text, "image_url": new_image_url}

        except Exception as e:
            logger.error(f"❌ Upload failed: {e}")
            try:
                screenshot_path = Config.TEMP_FILES_DIR / f"debug_error_{uuid.uuid4().hex[:8]}.png"
                await self.page.screenshot(path=str(screenshot_path), full_page=True)
                logger.error(f"📸 Error screenshot saved: {screenshot_path}")
            except: pass
            raise

    async def cleanup(self):
        """Deletes all created temporary files."""
        for fp in self.temp_files:
            try:
                if fp.exists():
                    fp.unlink()
                    logger.debug(f"🧹 Cleaned up: {fp}")
            except Exception as e:
                logger.warning(f"⚠️ Failed to delete {fp}: {e}")
        self.temp_files.clear()
