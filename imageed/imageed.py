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
#
#
"""
This Flask application acts as a proxy server that mimics an AI model endpoint
for upstream Openwebui or LiteLLM integration. It handles image editing requests by:
1. Receiving OpenAI-compatible image edit requests
2. Forwarding them to the Qwen API with proper authentication
3. Using Selenium to extract generated images from the Qwen chat interface
4. Returning results in a standardized format
The service is designed to run within isolated network namespaces with Chrome
running in headless mode for automated browser interactions.
Environment Variables:
    - QWEN_API_URL: Base URL for Qwen API endpoints
    - QWEN_MODEL: Model identifier for Qwen API
    - TOKENS_FILE_PATH: Path to JSON file containing authentication tokens
    - INSTANCE_IP: IP address of this instance within the network namespace
    - CHROME_DEBUG_PORT_EXTERNAL: External port for Chrome DevTools protocol
    - FLASK_HOST/FLASK_PORT: Flask server binding configuration
    - Various timeout and behavior flags (see Config class)
"""
from flask import Flask, request, jsonify
import requests
import time
import json
import os
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

# Load environment variables from .env file
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
    # Authentication Configuration
    # Path to JSON file containing session tokens and cookies
    TOKENS_FILE_PATH = os.getenv("TOKENS_FILE_PATH", "/root/ai/core/qwen/api3264/session/tokens.json")
    # Cache TTL for authentication headers in seconds
    CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))
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
        "INFO": Colors.CYAN,
        "SUCCESS": Colors.GREEN,
        "WARN": Colors.YELLOW,
        "ERROR": Colors.RED,
        "DEBUG": Colors.MAGENTA
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
# STATE & CACHE
# ==========================================
# Global cache for authentication headers to avoid frequent file reads
CACHED_HEADERS = None
CACHE_TIMESTAMP = 0

# ==========================================
# CORE FUNCTIONS
# ==========================================
def load_tokens_from_file():
    """
    Load authentication tokens and cookies from the tokens JSON file.
    Implements caching mechanism to reduce file I/O operations.
    Returns:
        dict: Headers dictionary with Authorization, Cookie, and other required headers
        None: If loading fails or no valid account found
    """
    global CACHED_HEADERS, CACHE_TIMESTAMP
    current_time = time.time()
    # Check if cached headers are still valid
    if CACHED_HEADERS and (current_time - CACHE_TIMESTAMP) < Config.CACHE_TTL:
        debug_log("Using cached authentication headers.")
        return CACHED_HEADERS
    try:
        debug_log(f"Loading tokens from {Config.TOKENS_FILE_PATH}...")
        # Validate tokens file exists
        if not os.path.exists(Config.TOKENS_FILE_PATH):
            log(f"Tokens file not found: {Config.TOKENS_FILE_PATH}", "WARN")
            return None
        # Read and parse JSON file
        with open(Config.TOKENS_FILE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # Validate data structure
        if not isinstance(data, list) or len(data) == 0:
            debug_log("Tokens file is empty or has invalid format.")
            return None
        # Find first valid (non-invalidated) account
        best_acc = None
        for acc in data:
            if not acc.get("invalid", False):
                best_acc = acc
                break
        if not best_acc:
            debug_log("No valid account found in tokens file.")
            return None
        # Extract token and cookies
        token = best_acc.get("token", "")
        cookies = best_acc.get("cookies", [])
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
        # Build headers dictionary
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "Authorization": f"Bearer {token}",
            "Cookie": cookie_str,
            "Referer": "https://chat.qwen.ai/"
        }
        debug_log(f"Token loaded: {token[:10]}...")
        debug_log(f"Cookies loaded: {len(cookies)} items.")
        # Update cache
        CACHED_HEADERS = headers
        CACHE_TIMESTAMP = current_time
        return headers
    except Exception as e:
        log(f"Failed to load tokens: {e}", "ERROR")
        return None

def get_driver():
    """
    Create and return a Selenium WebDriver instance connected to existing Chrome.
    Connects to Chrome via DevTools protocol using the configured debug port.
    Returns:
        webdriver.Chrome: Connected WebDriver instance
    """
    debug_log(f"Connecting to browser at {Config.CHROME_HOST}:{Config.CHROME_DEBUG_PORT}...")
    options = webdriver.ChromeOptions()
    options.add_experimental_option("debuggerAddress", f"{Config.CHROME_HOST}:{Config.CHROME_DEBUG_PORT}")
    return webdriver.Chrome(options=options)

def extract_image_url_from_chat(driver, timeout=None):
    """
    Extract the latest generated image URL from the Qwen chat interface.
    Waits for image elements matching the CDN pattern and returns the last one.
    Args:
        driver: Selenium WebDriver instance
        timeout: Optional timeout override for waiting
    Returns:
        str: Image URL if found
        None: If no image found or timeout occurs
    """
    if timeout is None:
        timeout = Config.TIMEOUT_IMAGE_EXTRACTION
    try:
        debug_log(f"Waiting for image elements (timeout={timeout}s)...")
        wait = WebDriverWait(driver, timeout)
        # Wait for image elements from Qwen CDN to appear
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "img[src*='cdn.qwenlm.ai']")))
        time.sleep(2)  # Additional delay to ensure all images are loaded
        # Find all matching images
        images = driver.find_elements(By.CSS_SELECTOR, "img[src*='cdn.qwenlm.ai']")
        debug_log(f"Found {len(images)} image(s).")
        if images:
            url = images[-1].get_attribute("src")
            debug_log(f"Last image src: {url[:100]}...")
            # Validate URL format
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
        url: Image URL to download
        headers: Optional headers for the request
    Returns:
        str: Base64 encoded image data
        None: If download or encoding fails
    """
    try:
        debug_log(f"Downloading image from {url[:80]}...")
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        b64_data = base64.b64encode(resp.content).decode('utf-8')
        debug_log(f"Downloaded {len(resp.content)} bytes, encoded to {len(b64_data)} chars.")
        log(f"Image encoded to base64 ({len(b64_data)} bytes).", "SUCCESS")
        return b64_data
    except Exception as e:
        log(f"Failed to download/encode image: {e}", "ERROR")
        return None

# ==========================================
# FLASK ROUTES
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
        JSON response with edited image data or error information
    """
    debug_log("=== REQUEST RECEIVED ===")
    # Parse incoming JSON request
    try:
        data = request.get_json()
        debug_log(f"Request JSON keys: {list(data.keys()) if data else 'None'}")
    except Exception as e:
        debug_log(f"JSON parse error: {e}")
        return jsonify({"error": "Invalid JSON"}), 400

    image_input = data.get("image")
    prompt = data.get("prompt")
    debug_log(f"Prompt: {prompt}")
    debug_log(f"Image input type: {type(image_input)}, length: {len(image_input) if isinstance(image_input, str) else 'N/A'}")

    # Validate required fields
    if not image_input or not prompt:
        return jsonify({"error": "Missing fields"}), 400

    # Process image input and detect format — ✅ EXTENDED SUPPORT
    if isinstance(image_input, str):
        # Step 1: Strip data URI prefix if present
        if "," in image_input:
            check = image_input.split(",", 1)[-1]
            uri_prefix = image_input.split(",", 1)[0]
        else:
            check = image_input
            uri_prefix = None

        # Step 2: Try to decode base64 to inspect real header (magic bytes)
        try:
            raw_bytes = base64.b64decode(check, validate=True)
            # Use imghdr to detect format reliably
            img_type = imghdr.what(None, h=raw_bytes)
            debug_log(f"Detected image type via imghdr: {img_type}")
        except Exception as e:
            debug_log(f"Base64 decode failed: {e} → falling back to signature check")
            img_type = None

        # Step 3: Fallback to signature-based detection (keep old logic for compatibility)
        if img_type is None:
            if check.startswith("/9j/"):
                img_type = "jpeg"
            elif check.startswith("iVBOR"):
                img_type = "png"
            elif check.startswith("UklGR"):
                img_type = "bmp"
            elif check.startswith("Qk"):
                img_type = "bmp"  # alternate BMP signature
            elif check.startswith("RIFF") and b"WEBP" in raw_bytes[:12]:
                img_type = "webp"
            elif check.startswith("GIF8"):
                img_type = "gif"
            elif check.startswith("II*") or check.startswith("MM*"):
                img_type = "tiff"
            else:
                img_type = "jpeg"  # default fallback

        # Step 4: Build correct data URI
        mime_map = {
            "jpeg": "image/jpeg",
            "jpg": "image/jpeg",
            "png": "image/png",
            "gif": "image/gif",
            "bmp": "image/bmp",
            "webp": "image/webp",
            "tiff": "image/tiff",
            "tif": "image/tiff"
        }
        mime = mime_map.get(img_type.lower(), "image/jpeg")
        image_data = f"data:{mime};base64,{check}"
        debug_log(f"Final image_data: {mime}, length={len(image_data)} chars.")
        debug_log(f"Preview: {image_data[:100]}...")
    else:
        return jsonify({"error": "Image must be string"}), 400

    # Load authentication headers
    headers = load_tokens_from_file()
    if not headers:
        return jsonify({"error": "Auth failed"}), 503

    # Build payload for Qwen API
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

    debug_log(f"Payload model: {payload['model']}")
    debug_log(f"Payload chat_type: {payload['chat_type']}")
    debug_log(f"Payload extra_body.task: {payload['extra_body']['task']}")
    debug_log(f"Payload image length: {len(image_data)}")

    driver = None
    try:
        # Send request to Qwen API
        log("Sending edit request to Qwen API...", "INFO")
        debug_log(f"POST {Config.QWEN_API_URL}")
        resp = requests.post(Config.QWEN_API_URL, json=payload, headers=headers, timeout=Config.REQUEST_TIMEOUT)
        debug_log(f"API Response status: {resp.status_code}")

        # Handle API errors
        if resp.status_code >= 400:
            debug_log(f"API Error body: {resp.text[:500]}")
            try:
                err_data = resp.json()
                msg = err_data.get('message', err_data.get('error', 'Unknown'))
                log(f"Qwen API error {resp.status_code}: {msg}", "ERROR")
                return jsonify({
                    "error": f"Qwen API error: {msg}",
                    "status_code": resp.status_code
                }), 502
            except:
                return jsonify({
                    "error": f"Qwen API returned {resp.status_code}",
                    "response": resp.text[:200]
                }), 502

        resp.raise_for_status()
        result = resp.json()
        debug_log(f"API Result keys: {list(result.keys())}")

        # Extract chat identifiers
        chat_id = result.get("chatId")
        parent_id = result.get("parentId")
        if not chat_id or not parent_id:
            debug_log(f"Missing chatId/parentId. chatId={chat_id}, parentId={parent_id}")
            return jsonify({"error": "API response missing chatId/parentId"}), 500

        log(f"Edit sent via API. chatId={chat_id}", "INFO")

        # Connect to browser and navigate to chat
        driver = get_driver()
        chat_url = f"https://chat.qwen.ai/c/{chat_id}"
        log(f"Navigating to chat: {chat_url}", "INFO")
        driver.get(chat_url)

        # Wait for page body to load
        try:
            WebDriverWait(driver, Config.DRIVER_CONNECT_TIMEOUT).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except TimeoutException:
            pass

        # Refresh page to trigger image generation display
        log("Refreshing page...", "INFO")
        time.sleep(3.0)
        driver.refresh()
        try:
            WebDriverWait(driver, Config.PAGE_LOAD_TIMEOUT).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except TimeoutException:
            pass

        # Extract image URL from DOM
        image_url = extract_image_url_from_chat(driver)
        if not image_url:
            return jsonify({
                "error": "Failed to retrieve image URL from DOM.",
                "note": "Navigation succeeded, but image not found."
            }), 500

        # Download and encode result image
        b64_result = download_and_encode_image(image_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"})
        if not b64_result:
            return jsonify({
                "error": "Failed to download or encode result image.",
                "note": "URL extracted, but download failed."
            }), 500

        debug_log("=== REQUEST COMPLETED SUCCESSFULLY ===")

        # Return standardized response
        return jsonify({
            "created": int(time.time()),
            "data": [{
                "download_url": image_url,
                "b64_json": b64_result
            }]
        })

    except requests.exceptions.RequestException as e:
        debug_log(f"RequestException: {e}")
        return jsonify({"error": f"API request failed: {str(e)}"}), 502
    except Exception as e:
        debug_log(f"Unhandled exception: {e}")
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
    # Validate critical files exist before starting
    if not os.path.exists(Config.TOKENS_FILE_PATH):
        log(f"⚠️  WARNING: Tokens file not found at {Config.TOKENS_FILE_PATH}", "WARN")
    # Start Flask server
    app.run(host=Config.FLASK_HOST, port=Config.FLASK_PORT, debug=False)
