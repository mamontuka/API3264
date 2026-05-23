# Copyright (C) 2026
#
# Authors:
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
Flask proxy for Qwen Image Editing with TokenBackend integration.
This application acts as a proxy server that mimics an AI model endpoint
for upstream OpenWebUI or LiteLLM integration. It handles image editing requests by:
1. Receiving OpenAI-compatible image edit requests.
2. Forwarding them to the Qwen API with proper authentication via TokenBackend.
3. Using Selenium to extract generated images from the Qwen chat interface.
4. Returning results in a standardized format.

The service is designed to run within isolated network namespaces with Chrome
running in headless mode for automated browser interactions.

Environment Variables:
- QWEN_API_URL: Base URL for Qwen API endpoints.
- QWEN_MODEL: Model identifier for Qwen API.
- TOKEN_BACKEND_MODE: Storage backend for tokens ('postgres', 'file', etc.).
- TOKENS_FILE_PATH: Path to JSON file containing authentication tokens (fallback).
- INSTANCE_IP: IP address of this instance within the network namespace.
- CHROME_DEBUG_PORT_EXTERNAL: External port for Chrome DevTools protocol.
- FLASK_HOST/FLASK_PORT: Flask server binding configuration.
- Various timeout and behavior flags (see Config class).
"""

import sys
import os

# 🔶 FIX: Add parent API directory to path for token_backends import
_API_CORE_DIR = "/root/ai/core/qwen/api3264"
if _API_CORE_DIR not in sys.path:
    sys.path.insert(0, _API_CORE_DIR)

from flask import Flask, request, jsonify
import requests
import time
import json
import base64
import imghdr
from io import BytesIO
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import asyncio

# 🔶 INTEGRATION: Import unified token backends from main API
try:
    from token_backends.factory import init_token_storage, get_token_backend
    from token_backends.base import TokenData
    TOKEN_BACKENDS_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] ⚠️ token_backends module not found: {e}", file=sys.stderr)
    TOKEN_BACKENDS_AVAILABLE = False

load_dotenv()
app = Flask(__name__)

# ==========================================
# CONFIGURATION MANAGER
# ==========================================
class Config:
    """
    Centralized configuration manager that loads all settings from environment variables.
    Provides type conversion and default values for all configuration parameters.
    """

    # API Configuration
    # Base URL for Qwen API chat completions endpoint
    QWEN_API_URL = os.getenv("QWEN_API_URL", "http://10.32.64.2:3264/api/chat/completions")
    # Model identifier to use for API requests
    QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen3.6-plus")

    # 🔶 NEW: Token backend config mapped to env
    # Storage backend mode for token management
    TOKEN_STORAGE_BACKEND = os.getenv("TOKEN_BACKEND_MODE", "postgres")
    # Path to JSON file containing session tokens and cookies (used for file fallback)
    TOKENS_FILE_PATH = os.getenv("TOKENS_FILE_PATH", "/root/ai/core/qwen/api3264/session/tokens.json")

    # Network Configuration
    # IP address of this instance within the network namespace
    INSTANCE_IP = os.getenv("INSTANCE_IP", "10.32.64.2")
    # Chrome host (same as instance IP in netns setup)
    CHROME_HOST = INSTANCE_IP
    # External Chrome debug port exposed via socat
    CHROME_DEBUG_PORT = int(os.getenv("CHROME_DEBUG_PORT_EXTERNAL", "9223"))

    # Flask server host binding
    FLASK_HOST = os.getenv("FLASK_HOST", "10.32.64.2")
    # Flask server port
    FLASK_PORT = int(os.getenv("FLASK_PORT", "7264"))

    # Timeout Configuration
    # Maximum time to wait for image extraction from DOM
    TIMEOUT_IMAGE_EXTRACTION = int(os.getenv("TIMEOUT_IMAGE_EXTRACTION", "45"))
    # Timeout for API requests to Qwen
    REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60"))
    # Timeout for initial driver connection
    DRIVER_CONNECT_TIMEOUT = int(os.getenv("DRIVER_CONNECT_TIMEOUT", "10"))
    # Timeout for page load operations
    PAGE_LOAD_TIMEOUT = int(os.getenv("PAGE_LOAD_TIMEOUT", "15"))

    # Feature Flags
    # Enable/disable debug logging
    DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
    # Enable/disable colored log output
    COLOR_LOGS = os.getenv("COLOR_LOGS", "true").lower() == "true"

# ==========================================
# LOGGING SYSTEM
# ==========================================
class Colors:
    """ANSI color codes for terminal output formatting."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"

def log(message, level="INFO", color=None):
    """
    Log message with optional color formatting based on log level.
    Args:
        message: The message string to log
        level: Log level (INFO, SUCCESS, WARN, ERROR, DEBUG)
        color: Optional override for color code
    """
    if not Config.COLOR_LOGS:
        print(f"[{level}] {message}")
        return
    color_map = {
        "INFO": Colors.CYAN, "SUCCESS": Colors.GREEN, "WARN": Colors.YELLOW,
        "ERROR": Colors.RED, "DEBUG": Colors.MAGENTA
    }
    c = color or color_map.get(level, Colors.RESET)
    print(f"{c}[{level}]{Colors.RESET} {message}")

def debug_log(message):
    """
    Log debug message only if DEBUG_MODE is enabled.
    Args:
        message: Debug message to log
    """
    if Config.DEBUG_MODE:
        log(message, "DEBUG")

# ==========================================
# 🔶 NEW: TOKEN BACKEND INTEGRATION
# ==========================================
_token_backend = None

def _get_headers_sync() -> dict | None:
    """
    Sync wrapper to get headers from async TokenBackend.
    Safe for Flask threads.
    Retrieves the first valid token and constructs headers with cookies.
    Returns:
        dict: Headers dictionary with Authorization, Cookie, and User-Agent.
        None: If no valid tokens are available or backend error occurs.
    """
    global _token_backend
    try:
        if _token_backend is None:
            return None

        tokens = asyncio.run(_token_backend.load_all())
        if not tokens:
            log("No tokens available in backend.", "WARN")
            return None

        valid_tokens = [t for t in tokens if not t.invalid]
        if not valid_tokens:
            log("No valid tokens found.", "WARN")
            return None

        token_data = valid_tokens[0]
        cookies = token_data.cookies or []
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])

        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "Authorization": f"Bearer {token_data.token}",
            "Cookie": cookie_str,
            "Referer": "https://chat.qwen.ai/"
        }
    except Exception as e:
        log(f"Token backend error: {e}", "ERROR")
        return None

# ==========================================
# FILE TOKEN LOADING (Fallback)
# ==========================================
def load_tokens_from_file():
    """
    File fallback loader preserved for compatibility.
    Load authentication tokens and cookies from the tokens JSON file.
    Returns:
        dict: Headers dictionary with Authorization, Cookie, and other required headers.
        None: If loading fails or no valid account found.
    """
    try:
        debug_log(f"Loading tokens from {Config.TOKENS_FILE_PATH}...")
        if not os.path.exists(Config.TOKENS_FILE_PATH):
            log(f"Tokens file not found: {Config.TOKENS_FILE_PATH}", "WARN")
            return None

        with open(Config.TOKENS_FILE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if not isinstance(data, list) or len(data) == 0:
            debug_log("Tokens file is empty or invalid.")
            return None

        best_acc = next((acc for acc in data if not acc.get("invalid", False)), None)
        if not best_acc:
            debug_log("No valid account found.")
            return None

        token = best_acc.get("token", "")
        cookies = best_acc.get("cookies", [])
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])

        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Authorization": f"Bearer {token}",
            "Cookie": cookie_str,
            "Referer": "https://chat.qwen.ai/"
        }
    except Exception as e:
        log(f"Failed to load tokens: {e}", "ERROR")
        return None

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint indicating token backend status."""
    return jsonify({
        "status": "ok",
        "token_backend": "active" if _token_backend else "inactive",
        "timestamp": int(time.time())
    })

# ==========================================
# CORE FUNCTIONS
# ==========================================
def get_driver():
    """
    Create and return a Selenium WebDriver instance connected to existing Chrome.
    Connects to Chrome via DevTools protocol using the configured debug port.
    Returns:
        webdriver.Chrome: Connected WebDriver instance.
    """
    debug_log(f"Connecting to browser at {Config.CHROME_HOST}:{Config.CHROME_DEBUG_PORT}...")
    options = webdriver.ChromeOptions()
    options.add_experimental_option("debuggerAddress", f"{Config.CHROME_HOST}:{Config.CHROME_DEBUG_PORT}")
    return webdriver.Chrome(options=options)

def extract_image_url_from_chat(driver, timeout=None):
    """
    Extract the latest generated image URL from the Qwen chat interface.
    Waits for image elements matching the CDN pattern and returns the last one.
    URL cleanup: removes OSS resize parameters to get original full size image result.
    Args:
        driver: Selenium WebDriver instance.
        timeout: Optional timeout override for waiting.
    Returns:
        str: Image URL if found.
        None: If no image found or timeout occurs.
    """
    if timeout is None:
        timeout = Config.TIMEOUT_IMAGE_EXTRACTION
    try:
        debug_log(f"Waiting for image elements (timeout={timeout}s)...")
        wait = WebDriverWait(driver, timeout)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "img[src*='cdn.qwenlm.ai']")))
        time.sleep(2)
        images = driver.find_elements(By.CSS_SELECTOR, "img[src*='cdn.qwenlm.ai']")
        debug_log(f"Found {len(images)} image(s).")
        if images:
            url = images[-1].get_attribute("src")
            if url and "&x-oss-process=" in url:
                url = url.split("&x-oss-process=", 1)[0]
                debug_log("Cleaned URL from resize params.")
            if url and url.startswith("https://cdn.qwenlm.ai/"):
                log(f"Extracted fresh URL: {url[:80]}...", "SUCCESS")
                return url
        log("No image found after refresh.", "WARN")
        return None
    except TimeoutException:
        log("Timeout waiting for image.", "ERROR")
        return None
    except Exception as e:
        log(f"Extraction failed: {e}", "ERROR")
        return None

def download_and_encode_image(url, headers=None):
    """
    Download image from URL and encode it as base64 string.
    Args:
        url: Image URL to download.
        headers: Optional headers for the request.
    Returns:
        str: Base64 encoded image data.
        None: If download or encoding fails.
    """
    try:
        debug_log(f"Downloading image from {url[:80]}...")
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        b64_data = base64.b64encode(resp.content).decode('utf-8')
        log(f"Image encoded to base64 ({len(b64_data)} bytes).", "SUCCESS")
        return b64_data
    except Exception as e:
        log(f"Failed to download/encode image: {e}", "ERROR")
        return None

# ==========================================
@app.route('/v1/images/edits', methods=['POST'])
def edit_image():
    """
    Handle image editing requests compatible with OpenAI Images Edits API.
    This endpoint allows LiteLLM to treat this proxy as a model endpoint.
    Request Format:
    {
        "image": "<base64_encoded_image>",
        "prompt": "<edit_instruction>"
    }
    Response Format:
    {
        "created": <timestamp>,
        "data": [{
            "download_url": "<image_url>",
            "b64_json": "<base64_result>"
        }]
    }
    Returns:
        JSON response with edited image data or error information.
    """
    debug_log("=== REQUEST RECEIVED ===")
    try:
        data = request.get_json()
        debug_log(f"Request JSON keys: {list(data.keys()) if data else 'None'}")
    except Exception as e:
        debug_log(f"JSON parse error: {e}")
        return jsonify({"error": "Invalid JSON"}), 400

    image_input = data.get("image")
    prompt = data.get("prompt")
    debug_log(f"Prompt: {prompt}")

    if not image_input or not prompt:
        return jsonify({"error": "Missing fields"}), 400

    # Process image input and detect format — ✅ EXTENDED SUPPORT
    if isinstance(image_input, str):
        # Step 1: Strip data URI prefix if present
        if "," in image_input:
            check = image_input.split(",", 1)[-1]
        else:
            check = image_input
        try:
            raw_bytes = base64.b64decode(check, validate=True)
            img_type = imghdr.what(None, h=raw_bytes)
            debug_log(f"Detected image type via imghdr: {img_type}")
        except Exception:
            img_type = None

        # Step 2: Fallback to signature-based detection
        if img_type is None:
            if check.startswith("/9j/"): img_type = "jpeg"
            elif check.startswith("iVBOR"): img_type = "png"
            elif check.startswith("UklGR"): img_type = "bmp"
            elif check.startswith("Qk"): img_type = "bmp"
            elif check.startswith("RIFF") and b"WEBP" in raw_bytes[:12]: img_type = "webp"
            elif check.startswith("GIF8"): img_type = "gif"
            elif check.startswith("II*") or check.startswith("MM*"): img_type = "tiff"
            else: img_type = "jpeg"

        mime_map = {
            "jpeg": "image/jpeg", "jpg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "bmp": "image/bmp", "webp": "image/webp",
            "tiff": "image/tiff", "tif": "image/tiff"
        }
        mime = mime_map.get(img_type.lower(), "image/jpeg")
        image_data = f"data:{mime};base64,{check}"
        debug_log(f"Final image_data: {mime}, length={len(image_data)} chars.")
    else:
        return jsonify({"error": "Image must be string"}), 400

    # 🔶 NEW: Get headers from TokenBackend
    headers = _get_headers_sync()
    if not headers:
        log("Token backend failed, trying file loader...", "WARN")
        headers = load_tokens_from_file()
        if not headers:
            return jsonify({"error": "Auth failed"}), 503

    payload = {
        "model": Config.QWEN_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": f"Instruction: {prompt}"},
                {"type": "image_url", "image_url": {"url": image_data}}
            ]
        }],
        "stream": False,
        "chat_type": "t2v",
        "extra_body": {
            "task": "image-edit",
            "enable_edit": True,
            "image": image_data
        }
    }

    driver = None
    try:
        log("Sending edit request to Qwen API...", "INFO")
        resp = requests.post(Config.QWEN_API_URL, json=payload, headers=headers, timeout=Config.REQUEST_TIMEOUT)
        debug_log(f"API Response status: {resp.status_code}")

        if resp.status_code >= 400:
            try:
                err_data = resp.json()
                msg = err_data.get('message', err_data.get('error', 'Unknown'))
                return jsonify({"error": f"Qwen API error: {msg}", "status_code": resp.status_code}), 502
            except:
                return jsonify({"error": f"Qwen API returned {resp.status_code}", "response": resp.text[:200]}), 502

        resp.raise_for_status()
        result = resp.json()
        chat_id = result.get("chatId")
        parent_id = result.get("parentId")
        if not chat_id or not parent_id:
            return jsonify({"error": "API response missing chatId/parentId"}), 500
        log(f"Edit sent via API. chatId={chat_id}", "INFO")

        driver = get_driver()
        chat_url = f"https://chat.qwen.ai/c/{chat_id}"
        log(f"Navigating to chat: {chat_url}", "INFO")
        driver.get(chat_url)

        try:
            WebDriverWait(driver, Config.DRIVER_CONNECT_TIMEOUT).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except TimeoutException:
            pass

        log("Refreshing page...", "INFO")
        time.sleep(3.0)
        driver.refresh()
        try:
            WebDriverWait(driver, Config.PAGE_LOAD_TIMEOUT).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except TimeoutException:
            pass

        image_url = extract_image_url_from_chat(driver)
        if not image_url:
            try:
                response_divs = driver.find_elements(By.CSS_SELECTOR, "div.qwen-response-message")
                if response_divs:
                    fallback_text = response_divs[-1].get_attribute("innerText").strip()
                    if fallback_text:
                        return jsonify({
                            "error": "Failed to retrieve image URL from DOM.",
                            "note": "Navigation succeeded, but image not found.",
                            "qwen_message": fallback_text
                        }), 500

                messages = driver.find_elements(By.CSS_SELECTOR,
                    "div[class*='message-content'], div[class*='markdown-body'], span[class*='qwen-markdown-text']")
                if messages:
                    fallback_text = messages[-1].text.strip()
                    if fallback_text:
                        return jsonify({
                            "error": "Failed to retrieve image URL from DOM.",
                            "note": "Navigation succeeded, but image not found.",
                            "qwen_message": fallback_text
                        }), 500
            except Exception:
                pass
            return jsonify({"error": "No image URL extracted."}), 500

        b64_result = download_and_encode_image(image_url, headers={"User-Agent": "Mozilla/5.0"})
        if not b64_result:
            return jsonify({"error": "Failed to download or encode result image."}), 500

        debug_log("=== REQUEST COMPLETED SUCCESSFULLY ===")
        return jsonify({
            "created": int(time.time()),
            "data": [{
                "download_url": image_url,
                "b64_json": b64_result
            }]
        })
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"API request failed: {str(e)}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        # Note: Driver is not closed to maintain connection pool efficiency
        if driver:
            pass

# ==========================================
# MAIN ENTRY POINT
# ==========================================
if __name__ == '__main__':
    log("🚀 Qwen Edit Proxy starting...", "INFO")
    log(f"📡 Listening on http://{Config.FLASK_HOST}:{Config.FLASK_PORT}", "INFO")
    log(f"🔧 DEBUG_MODE={'ON' if Config.DEBUG_MODE else 'OFF'}", "INFO")
    log(f"🌐 Chrome Debug: {Config.CHROME_HOST}:{Config.CHROME_DEBUG_PORT}", "INFO")

    # 🔶 NEW: Initialize token backend
    if TOKEN_BACKENDS_AVAILABLE:
        try:
            _token_backend = asyncio.run(init_token_storage())
            if _token_backend:
                log(f"✅ TokenStorage initialized: {_token_backend.__class__.__name__}", "SUCCESS")
            else:
                log("⚠️ TokenStorage init returned None, fallback will be used.", "WARN")
        except Exception as e:
            log(f"❌ TokenStorage init failed: {e}", "ERROR")
            _token_backend = None
    else:
        log("⚠️ token_backends module not found, using file mode.", "WARN")
        _token_backend = None

    # Validate critical files exist before starting
    if not os.path.exists(Config.TOKENS_FILE_PATH):
        log(f"⚠️  WARNING: Tokens file not found at {Config.TOKENS_FILE_PATH}", "WARN")

    app.run(host=Config.FLASK_HOST, port=Config.FLASK_PORT, debug=False)
