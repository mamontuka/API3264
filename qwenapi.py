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
FreeQwenApi - OpenAI-compatible proxy for Qwen Chat
====================================================
This module implements a FastAPI-based proxy server that translates OpenAI-compatible
API requests into Qwen Chat API calls. It handles:
- Chat session management and persistence
- Token authentication and rotation
- Streaming responses in Server-Sent Events (SSE) format
- Retry logic for transient errors (e.g., "chat in progress")
- Database integration with OpenWebUI for chat ID mapping
- Error handling and fallback responses
Architecture:
    OpenWebUI/LiteLLM → FreeQwenApi → Qwen Chat API
                        ↑
                This module (qwenapi.py)
Key Features:
    • OpenAI API compatibility (POST /v1/chat/completions)
    • Streaming support with proper SSE formatting
    • Persistent chat state mapping (OpenWebUI chat_id ↔ Qwen chat_id)
    • Automatic retry with exponential backoff for "chat in progress" errors
    • PostgreSQL integration with asyncpg (OpenWebUI chat lookup)
    • Token management with round-robin rotation and rate limiting
    • Comprehensive logging for debugging and monitoring
Usage:
    1. Configure environment variables in .env or config.py
    2. Run: python qwenapi.py --start-proxy --host 0.0.0.0 --port 3269
    3. Point your OpenAI-compatible client to http://<host>:3269/api
Author: Oleh Mamont et al.
License: GPLv3
"""
import os
import json
import time
import uuid
import asyncio
import sys
import logging
import argparse
import hashlib
from datetime import datetime
from typing import List, Optional, Dict, Any, Union
from contextlib import asynccontextmanager
import httpx
import uvicorn
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright
from pydantic import BaseModel

# Import our configuration module
# Config contains: HTTP settings, paths, model mappings, DB params, etc.
from config import Config, setup_logging

# 🔥 NEW: Import async database functions
from db_async import init_db_pool, close_db_pool, fetch_chat_id_from_db, test_db_connection
# 🔥 UPDATED: Import new ChatState factory
from chat_state.factory import init_chat_state, close_chat_state, get_chat_state_backend, is_fallback_active

# =================================================================
# INITIALIZATION
# =================================================================
# Ensure all required directories exist before starting the application
# Creates: session/, logs/, and any other paths defined in Config
Config.ensure_dirs()

# Configure logging according to Config settings (level, format, file output)
# Returns a logger instance configured for this module
logger = setup_logging()

# Create a persistent HTTP client for making requests to Qwen API
# - timeout: Maximum time to wait for a response (from Config)
# - follow_redirects: Whether to automatically follow HTTP redirects
http_client = httpx.AsyncClient(
    timeout=Config.HTTP_TIMEOUT,
    follow_redirects=Config.HTTP_FOLLOW_REDIRECTS
)

# 🔥 REMOVED: Global CHAT_STATE dictionary and lock.
# State management is now handled by the backend via get_chat_state_backend().
# This supports both File and PostgreSQL modes with automatic fallback.

# =================================================================
# CHAT MAPPING
# =================================================================
async def get_or_create_qwen_chat(token_obj, openweb_chat_id: str, model: str):
    """
    Get existing Qwen chat ID or create a new one for the given OpenWebUI chat.
    This is the core function for maintaining conversation continuity:
    1. Check if we already have a Qwen chat ID for this OpenWebUI chat
    2. If not, create a new chat on Qwen side and store the mapping
    3. Return the Qwen chat ID for use in subsequent API calls

    🔥 UPDATED: Uses backend abstraction (File or PostgreSQL) for state persistence.

    Args:
        token_obj: Authentication token dictionary from load_tokens()
        openweb_chat_id: Unique identifier from OpenWebUI (UUID format)
        model: Model name to use for the chat (e.g., "qwen3.5-plus")
    Returns:
        str|None: Qwen chat ID if successful, None if creation failed
    Side effects:
        - May create a new chat via Qwen API
        - Updates state via backend (persistent storage)
        - Logs creation/loading operations
    """
    openweb_chat_id = str(openweb_chat_id).strip()

    # Get backend instance
    backend = get_chat_state_backend()

    # Check if we already have a mapping for this OpenWebUI chat
    state = await backend.get(openweb_chat_id)
    if state and state.qwen_chat_id:
        logger.debug(f"Found existing chat: {openweb_chat_id} -> {state.qwen_chat_id}")
        return state.qwen_chat_id

    # No existing mapping: create new chat on Qwen side
    logger.info(f"Creating new Qwen chat for {openweb_chat_id}, model: {model}")
    qwen_chat_id = await create_qwen_chat(token_obj, model)
    if not qwen_chat_id:
        logger.error(f"Failed to create chat for {openweb_chat_id}")
        return None

    # Store the new mapping via backend
    from chat_state.base import ChatStateData
    new_state = ChatStateData(
        qwen_chat_id=qwen_chat_id,
        last_parent_id=None,
        is_new=True,
        created_at=time.time()
    )
    await backend.set(openweb_chat_id, new_state)

    logger.info(f"Created and saved chat: {openweb_chat_id} -> {qwen_chat_id}")

    # 🔥 IMPORTANT: Delay AFTER saving state
    # This gives Qwen time to fully initialize the new chat before first message
    await asyncio.sleep(2.0)
    return qwen_chat_id

async def update_chat_parent_id(openweb_chat_id: str, new_parent_id: str):
    """
    Update the last_parent_id for a chat after successful response.
    The parent_id is used by Qwen API to maintain message threading within a chat.
    We store the last successful response ID so subsequent messages can reference it.

    🔥 UPDATED: Uses backend abstraction for state updates.

    Args:
        openweb_chat_id: OpenWebUI chat identifier
        new_parent_id: Response ID from Qwen API to use as parent for next message
    Side effects:
        - Updates state via backend
        - Logs update operation
    """
    backend = get_chat_state_backend()
    await backend.update_parent(openweb_chat_id, new_parent_id)
    logger.debug(f"Updated: last_parent_id[{openweb_chat_id[:8]}...] = {new_parent_id[:8]}...")

def get_mapped_model(model_name: str) -> str:
    """
    Get the actual Qwen model name for a given alias.
    Allows users to request models by friendly names (e.g., "qwen-max")
    while the proxy translates to the actual API model name (e.g., "qwen3.5-plus").
    Args:
        model_name: Model name from client request (case-insensitive)
    Returns:
        str: Mapped model name if found in Config.MODEL_MAPPING, else original name
    """
    return Config.MODEL_MAPPING.get(model_name.lower(), model_name)

def load_available_models() -> List[str]:
    """
    Load list of available models from configuration and file.
    Combines:
    1. Models defined in Config.MODEL_MAPPING keys
    2. Default model from Config.DEFAULT_MODEL
    3. Additional models listed in Config.AVAILABLE_MODELS_FILE (one per line)
    Returns:
        List[str]: Sorted list of available model names
    """
    models = set(Config.MODEL_MAPPING.keys())
    models.add(Config.DEFAULT_MODEL)
    # Load additional models from file if it exists
    if Config.AVAILABLE_MODELS_FILE.exists():
        try:
            with open(Config.AVAILABLE_MODELS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    value = line.strip()
                    # Skip empty lines and comments
                    if value and not value.startswith("#"):
                        models.add(value)
        except Exception as e:
            logger.warning(f"Failed to load models from {Config.AVAILABLE_MODELS_FILE}: {e}")
    return sorted(models)

# =================================================================
# TOKEN MANAGEMENT
# =================================================================
def load_tokens():
    """
    Load authentication tokens from persistent storage.
    Tokens are stored as a list of dictionaries with structure:
    {
        "id": str,           # Unique identifier for this token/account
        "token": str,        # Actual Qwen authentication token
        "cookies": list,     # Browser cookies for session persistence
        "added_at": str,     # ISO timestamp when token was added
        "invalid": bool,     # Flag: should this token be skipped?
        "resetAt": str|None  # ISO timestamp: when rate limit resets (if limited)
    }
    Returns:
        List[Dict]: List of token dictionaries, or empty list if file missing/error
    """
    Config.ensure_dirs()
    if not Config.TOKENS_FILE.exists():
        return []
    try:
        with open(Config.TOKENS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading {Config.TOKENS_FILE}: {e}")
        return []

def save_tokens(tokens):
    """
    Save authentication tokens to persistent storage.
    Args:
        tokens: List of token dictionaries to save
    Side effects:
        - Writes tokens to Config.TOKENS_FILE (overwrites existing)
        - Logs errors if save fails
    """
    Config.ensure_dirs()
    try:
        with open(Config.TOKENS_FILE, "w", encoding="utf-8") as f:
            json.dump(tokens, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving {Config.TOKENS_FILE}: {e}")

# Global pointer for round-robin token selection
_pointer = 0

def get_available_token():
    """
    Get next available authentication token using round-robin selection.
    Filters out:
    - Tokens marked as "invalid"
    - Tokens with resetAt in the future (still rate-limited)
    Returns:
        Dict|None: Next available token dictionary, or None if no valid tokens
    """
    global _pointer
    tokens = load_tokens()
    now = time.time() * 1000  # Convert to milliseconds for comparison
    # Filter: keep only tokens that are valid and not currently rate-limited
    valid = [t for t in tokens if not t.get('invalid') and (not t.get('resetAt') or datetime.fromisoformat(t['resetAt'].replace('Z', '+00:00')).timestamp() * 1000 <= now)]
    if not valid:
        return None
    # Round-robin: select next token and advance pointer
    token_obj = valid[_pointer % len(valid)]
    _pointer = (_pointer + 1) % len(valid)
    return token_obj

def mark_rate_limited(token_id, hours=24):
    """
    Mark a token as rate-limited, preventing its use until reset time.
    Args:
        token_id: Identifier of the token to mark (matches token["id"])
        hours: Number of hours until the token should be available again (default: 24)
    Side effects:
        - Updates token["resetAt"] to current time + hours
        - Saves updated tokens list to disk
    """
    tokens = load_tokens()
    for t in tokens:
        if t['id'] == token_id:
            # Calculate reset time: now + specified hours
            reset_time = datetime.fromtimestamp(time.time() + hours * 3600)
            t['resetAt'] = reset_time.isoformat() + "Z"  # ISO format with Z suffix
            break
    save_tokens(tokens)

# =================================================================
# AUTH & BROWSER
# =================================================================
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
            
# =================================================================
# CORE PROXY ENGINE
# =================================================================
async def create_qwen_chat(token_obj, model=Config.DEFAULT_MODEL):
    """
    Create a new chat session on Qwen side via API.
    Args:
        token_obj: Authentication token dictionary
        model: Model name to use for the new chat
    Returns:
        str|None: New chat ID from Qwen API, or None if creation failed
    Raises:
        Logs errors but doesn't raise exceptions (caller handles None return)
    """
    # 🔍 DEBUG: Function entry with context
    token_id = token_obj.get('id', 'unknown')
    token_preview = token_obj['token'][:8] + '...' if token_obj.get('token') else 'None'
    logger.debug(f"🔧 create_qwen_chat() called | token_id={token_id}, token={token_preview}, model={model}")

    token = token_obj['token']

    # Set cookies from token_obj for session persistence
    if 'cookies' in token_obj:
        cookies = token_obj['cookies']
        logger.debug(f"🍪 Setting {len(cookies)} cookies for token {token_id}")
        for idx, cookie in enumerate(cookies):
            cookie_name = cookie.get('name', 'unknown')
            cookie_domain = cookie.get('domain', 'unknown')
            logger.debug(f"🍪 Cookie[{idx}]: name={cookie_name}, domain={cookie_domain}")
            if cookie['name'] not in http_client.cookies:
                http_client.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'])
                logger.debug(f"🍪 Added cookie: {cookie_name}")
            else:
                logger.debug(f"🍪 Cookie already exists: {cookie_name}")
    else:
        logger.debug(f"🍪 No cookies in token_obj for token {token_id}")

    # Build request headers for Qwen API
    headers = {
        "Content-Type": "application/json", "Authorization": f"Bearer {token}",
        "Accept": "*/*", "User-Agent": Config.DEFAULT_HEADERS["User-Agent"],
        "Accept-Language": Config.DEFAULT_HEADERS["Accept-Language"],
        "Origin": Config.QWEN_BASE_URL, "Referer": Config.CHAT_PAGE_URL,
    }
    logger.debug(f"📤 Request headers keys: {list(headers.keys())}")
    logger.debug(f"📤 Authorization header: Bearer {token[:8]}...")

    # Payload for creating a new chat
    payload = {
        "title": "New Chat", "models": [model], "chat_mode": "normal",
        "chat_type": "t2t", "timestamp": int(time.time() * 1000)
    }
    logger.debug(f"📤 Request payload: {json.dumps(payload, ensure_ascii=False)}")
    logger.debug(f"🌐 Request URL: {Config.CREATE_CHAT_URL}")

    try:
        logger.info(f"🚀 Sending POST request to create chat... (timeout=30.0s)")
        start_time = time.time()

        resp = await http_client.post(Config.CREATE_CHAT_URL, headers=headers, json=payload, timeout=30.0)

        elapsed = time.time() - start_time
        logger.info(f"📥 Response received in {elapsed:.2f}s | status_code={resp.status_code}")

        # 🔍 DEBUG: Log all response headers
        logger.debug(f"📥 Response headers: {dict(resp.headers)}")

        # Check for x-actual-status-code (Qwen-specific)
        actual_status = resp.headers.get("x-actual-status-code")
        if actual_status:
            logger.debug(f"📥 x-actual-status-code: {actual_status}")

        # 🔍 DEBUG: Log response body preview
        resp_text = resp.text
        body_preview = resp_text[:1000] if len(resp_text) > 1000 else resp_text
        logger.debug(f"📥 Response body preview:\n{body_preview}")

        if resp.status_code == 200:
            content_type = resp.headers.get("content-type", "")
            logger.debug(f"📥 Content-Type: {content_type}")

            if "application/json" not in content_type:
                logger.error(f"❌ Unexpected content-type: {content_type}")
                logger.error(f"❌ Response body: {resp_text[:500]}")
                logger.debug(f"💡 Expected JSON but got different content type. Possible API change or error page.")
                return None

            try:
                logger.debug(f"🔍 Parsing JSON response...")
                data = resp.json()
                logger.debug(f"📦 Parsed JSON structure: {json.dumps(data, ensure_ascii=False)[:500]}")

                # Check for error indicators in response
                if data.get("success") is False:
                    logger.error(f"❌ API returned success=false")
                    logger.error(f"❌ Error details: {json.dumps(data, ensure_ascii=False)}")
                    return None

                if data.get("code"):
                    logger.error(f"❌ API returned error code: {data.get('code')}")
                    logger.error(f"❌ Full response: {json.dumps(data, ensure_ascii=False)}")
                    return None

                # Extract chat_id
                data_section = data.get('data', {})
                logger.debug(f"🔍 data section: {data_section}")

                if not isinstance(data_section, dict):
                    logger.error(f"❌ 'data' field is not a dict: type={type(data_section)}, value={data_section}")
                    return None

                chat_id = data_section.get('id')
                logger.debug(f"🔍 Extracted chat_id: {chat_id}")

                if chat_id:
                    logger.info(f"✅ Chat created successfully on Qwen: {chat_id}")
                    logger.debug(f"✅ Full response data: {json.dumps(data, ensure_ascii=False)}")
                    return chat_id
                else:
                    logger.error(f"❌ No 'id' field in response data!")
                    logger.error(f"❌ Available keys in data: {list(data_section.keys()) if isinstance(data_section, dict) else 'N/A'}")
                    logger.error(f"❌ Full response: {json.dumps(data, ensure_ascii=False)}")
                    return None

            except json.JSONDecodeError as je:
                logger.error(f"❌ JSON decode error: {je}")
                logger.error(f"❌ Raw response body: {resp_text[:500]}")
                logger.debug(f"💡 Response is not valid JSON. Possible HTML error page or API change.")
                return None
            except Exception as je:
                logger.error(f"❌ Unexpected error parsing response: {type(je).__name__}: {je}")
                logger.error(f"❌ Raw response body: {resp_text[:500]}")
                return None
        else:
            logger.error(f"❌ Chat creation failed: HTTP {resp.status_code}")
            logger.error(f"❌ Response body: {resp_text[:500]}")

            if actual_status:
                logger.error(f"❌ x-actual-status-code: {actual_status}")

            if resp.status_code >= 400:
                logger.debug(f"🔍 Attempting to parse error details from response...")
                try:
                    err_data = resp.json()
                    logger.error(f"❌ Error details (JSON): {json.dumps(err_data, ensure_ascii=False)}")

                    # Check for specific error patterns
                    if err_data.get("code") == "RateLimited":
                        logger.error(f"🚫 RATE LIMITED! Token {token_id} is rate limited.")
                        logger.debug(f"💡 Consider marking token as rate limited or switching to another token.")
                    elif err_data.get("code") == "Unauthorized" or resp.status_code == 401:
                        logger.error(f"🚫 UNAUTHORIZED! Token {token_id} may be invalid or expired.")
                        logger.debug(f"💡 Token needs refresh or re-login.")
                    elif err_data.get("message"):
                        logger.error(f"❌ Error message: {err_data.get('message')}")

                except json.JSONDecodeError:
                    logger.error(f"❌ Error response is not valid JSON: {resp_text[:300]}")
                except Exception as parse_err:
                    logger.error(f"❌ Error parsing error response: {parse_err}")

            # 🔍 DEBUG: Suggest possible causes
            logger.debug(f"💡 Possible causes:")
            logger.debug(f"💡   - Token expired or invalid")
            logger.debug(f"💡   - Model '{model}' not supported for chat creation")
            logger.debug(f"💡   - Rate limit exceeded")
            logger.debug(f"💡   - API endpoint changed")
            logger.debug(f"💡   - Network/proxy issues")

    except httpx.TimeoutException as e:
        logger.error(f"⏰ Timeout exception: {e}")
        logger.debug(f"💡 Request timed out after 30s. Qwen API may be slow or unreachable.")
    except httpx.ConnectError as e:
        logger.error(f"🔌 Connection error: {e}")
        logger.debug(f"💡 Cannot connect to Qwen API. Check network connectivity.")
    except httpx.HTTPError as e:
        logger.error(f"🌐 HTTP error: {type(e).__name__}: {e}")
    except Exception as e:
        logger.error(f"💥 Exception creating chat: {type(e).__name__}: {e}")
        logger.debug(f"📋 Full exception details:", exc_info=True)

    logger.debug(f"🔚 create_qwen_chat() returning None")
    return None

def build_qwen_payload(message_content, model, chat_id, parent_id=None, system_message=None, files=None):
    """
    Build request payload for Qwen Chat API.
    Translates OpenAI-style message format into Qwen's expected structure.
    Args:
        message_content: Content of the user message (string or list of content parts)
        model: Model name to use
        chat_id: Qwen chat ID to send message to
        parent_id: Optional parent message ID for threading (usually None for Qwen API v2)
        system_message: Optional system prompt to prepend
        files: Optional list of file attachments
    Returns:
        Dict: Payload dictionary ready for POST to Qwen Chat API
    """
    user_msg_id = str(uuid.uuid4())
    assistant_msg_id = str(uuid.uuid4())
    # Build message object in Qwen's format
    new_message = {
        "fid": user_msg_id, "parentId": parent_id, "parent_id": parent_id,
        "role": "user", "content": message_content, "chat_type": "t2t",
        "sub_chat_type": "t2t", "timestamp": int(time.time()), "user_action": "chat",
        "models": [model], "files": files or [], "childrenIds": [assistant_msg_id],
        "extra": {"meta": {"subChatType": "t2t"}},
        "feature_config": {"thinking_enabled": False, "output_schema": "phase"}
    }
    # Build full request payload
    payload = {
        "stream": True, "incremental_output": True, "chat_id": chat_id,
        "chat_mode": "normal", "messages": [new_message], "model": model,
        "parent_id": parent_id, "timestamp": int(time.time())
    }
    if system_message:
        payload["system_message"] = system_message
    return payload

def _normalize_message_content(content):
    """
    Normalize message content to Qwen API format.
    Handles both simple string content and complex content arrays
    (e.g., text + images) by converting to Qwen's expected structure.
    Args:
        content: Message content (str or list of content part dicts)
    Returns:
        Normalized content in Qwen-compatible format
    """
    if not isinstance(content, list):
        return content
    normalized = []
    for item in content:
        if not isinstance(item, dict):
            normalized.append(item)
            continue
        item_type = item.get("type")
        if item_type == "text" and isinstance(item.get("text"), str):
            normalized.append({"type": "text", "text": item["text"]})
        elif item_type == "image_url" and isinstance(item.get("image_url"), dict):
            url = item["image_url"].get("url")
            if url:
                normalized.append({"type": "image", "image": url})
        elif item_type == "image" and isinstance(item.get("image"), str):
            normalized.append({"type": "image", "image": item["image"]})
        elif item_type == "file" and isinstance(item.get("file"), str):
            normalized.append({"type": "file", "file": item["file"]})
        else:
            normalized.append(item)
    return normalized

def _extract_messages(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extract messages list from request body.
    Handles different request formats:
    - Standard OpenAI: body["messages"] (list)
    - Alternative: body["message"] (single message)
    Args:
        body: Parsed JSON request body
    Returns:
        List[Dict]: List of message dictionaries, or empty list if none found
    """
    messages = body.get("messages")
    if isinstance(messages, list) and messages:
        return messages
    if body.get("message") is not None:
        return [{"role": "user", "content": body.get("message")}]
    return []

def _extract_chat_ids(body: Dict[str, Any]):
    """
    Extract chat_id and parent_id from request body.
    Supports multiple API formats:
    - OpenAI: body["chat_id"], body["parent_id"]
    - OpenWebUI: nested fields, custom headers
    - LibreChat: alternative field names
    Args:
        body: Parsed JSON request body
    Returns:
        Tuple[str|None, str|None]: (chat_id, parent_id) or (None, None) if not found
    """
    # Check top-level fields first
    chat_id = None
    for field in Config.get_chat_id_fields():
        if body.get(field):
            chat_id = body[field]
            break
    # Check nested fields if not found at top level
    if not chat_id:
        for parent_key, child_key in Config.get_nested_chat_id_paths():
            parent = body.get(parent_key)
            if isinstance(parent, dict) and parent.get(child_key):
                chat_id = parent[child_key]
                break
    # Extract parent_id using similar logic
    parent_id = None
    for field in ["parentId", "parent_id", "x_qwen_parent_id", "message_id"]:
        if body.get(field):
            parent_id = body[field]
            break
    if not parent_id:
        for parent_key, child_key in Config.get_nested_chat_id_paths():
            parent = body.get(parent_key)
            if isinstance(parent, dict) and parent.get(child_key):
                parent_id = parent[child_key]
                break
    return chat_id, parent_id

async def _get_openwebui_chat_id_from_db(user_id: str, conversation_title: Optional[str] = None) -> Optional[str]:
    """
    Get stable chat ID from OpenWebUI PostgreSQL database.
    Queries the "chat" table to find the most recently updated chat
    for the given user, enabling automatic chat binding without explicit IDs.

    🔥 UPDATED: Now uses asyncpg (non-blocking) via db_async module.

    Args:
        user_id: OpenWebUI user identifier (from request headers)
        conversation_title: Optional title to filter by (not currently used)
    Returns:
        str|None: Chat ID from database, or None if not found/error
    Note:
        Table schema: chat(id TEXT, user_id TEXT, title TEXT, updated_at BIGINT, ...)
    """
    if not Config.OPENWEBUI_DB_ENABLED:
        return None

    # 🔥 ASYNC CALL - does NOT block Event Loop
    db_chat_id = await fetch_chat_id_from_db(user_id, conversation_title)
    return db_chat_id

def _generate_openweb_chat_id(request: Request, body: Dict[str, Any], model: str) -> str:
    """
    Generate/extract chat_id for OpenWebUI with priority order.
    Priority (highest to lowest):
    1. Explicit conversation_id/chat_id from request body or headers
    2. ID from OpenWebUI PostgreSQL database (auto-binding)
    3. Stable hash based on user_id + model + hour (for dialogue continuation)
    4. Fallback: random UUID (only if no user_id available)
    Args:
        request: FastAPI Request object (for headers)
        body: Parsed JSON request body
        model: Model name (used in stable hash generation)
    Returns:
        str: Deterministic or random chat ID for this request
    """
    # Priority 1: Check explicit fields in request (new chats from OpenWebUI)
    for field in ["conversation_id", "conversationId", "chatId", "chat_id", "thread_id", "threadId"]:
        if body.get(field):
            logger.debug(f"🔍 Using explicit {field}: {body[field][:8]}...")
            return str(body[field])
    # Check headers for chat ID
    for header in ["x-chat-id", "x-conversation-id", "openwebui-chat-id", "x-openwebui-chat-id"]:
        if request.headers.get(header):
            logger.debug(f"🔍 Using header {header}: {request.headers[header][:8]}...")
            return str(request.headers[header])
    # Check nested fields in body
    for parent_key, child_key in Config.get_nested_chat_id_paths():
        parent = body.get(parent_key)
        if isinstance(parent, dict) and parent.get(child_key):
            logger.debug(f"🔍 Using nested {parent_key}.{child_key}: {parent[child_key][:8]}...")
            return str(parent[child_key])

    # Priority 2: Try to get ID from OpenWebUI DB (auto-binding)
    # NOTE: This function is now called from an async context in handle_chat_completions
    # so we can await the DB call there. For this sync wrapper, we return None if DB is needed
    # to force the caller to use the async version.
    # However, to keep logic simple, we will handle the await in the main handler.
    # Returning a placeholder here if DB is strictly required in this sync function would break things.
    # Instead, we assume this function is only called where DB result isn't critical OR
    # we refactor the caller to be fully async.
    # 🔥 Since this function is called inside handle_chat_completions (which is async),
    # we should make THIS function async too. See handle_chat_completions for the await call.

    # Fallback to stable hash or random UUID if DB not checked here
    user_id = request.headers.get(Config.OPENWEBUI_USER_ID_HEADER)

    # Priority 3: Generate stable hash for dialogue continuation (DEFAULT)
    # Groups messages from same user + model within same hour into same chat
    if user_id:
        hour_bucket = int(time.time() // 3600)  # Group by hour
        stable_key = f"{user_id}:{model}:{hour_bucket}"
        stable_id = hashlib.sha256(stable_key.encode()).hexdigest()[:32]
        logger.debug(f"🔁 Using stable chat_id: {stable_id[:8]}... (user={user_id[:8]}, model={model}, hour={hour_bucket})")
        return stable_id

    # Priority 4: Fallback to random UUID (only if no user_id available)
    fallback_id = str(uuid.uuid4())
    logger.debug(f"⚠️ Fallback to random UUID: {fallback_id[:8]}... (no user_id)")
    return fallback_id

async def _generate_openweb_chat_id_async(request: Request, body: Dict[str, Any], model: str) -> str:
    """
    Async version of chat_id generation including DB lookup.
    """
    # Priority 1: Check explicit fields
    for field in ["conversation_id", "conversationId", "chatId", "chat_id", "thread_id", "threadId"]:
        if body.get(field):
            logger.debug(f"🔍 Using explicit {field}: {body[field][:8]}...")
            return str(body[field])

    for header in ["x-chat-id", "x-conversation-id", "openwebui-chat-id", "x-openwebui-chat-id"]:
        if request.headers.get(header):
            logger.debug(f"🔍 Using header {header}: {request.headers[header][:8]}...")
            return str(request.headers[header])

    for parent_key, child_key in Config.get_nested_chat_id_paths():
        parent = body.get(parent_key)
        if isinstance(parent, dict) and parent.get(child_key):
            logger.debug(f"🔍 Using nested {parent_key}.{child_key}: {parent[child_key][:8]}...")
            return str(parent[child_key])

    # Priority 2: DB Lookup (ASYNC)
    user_id = request.headers.get(Config.OPENWEBUI_USER_ID_HEADER)
    if user_id and Config.OPENWEBUI_DB_ENABLED:
        db_chat_id = await _get_openwebui_chat_id_from_db(user_id, body.get("title"))
        if db_chat_id:
            logger.debug(f"🗄 Using chat_id from DB: {db_chat_id[:8]}...")
            return db_chat_id

    # Priority 3: Stable Hash
    if user_id:
        hour_bucket = int(time.time() // 3600)
        stable_key = f"{user_id}:{model}:{hour_bucket}"
        stable_id = hashlib.sha256(stable_key.encode()).hexdigest()[:32]
        logger.debug(f"🔁 Using stable chat_id: {stable_id[:8]}...")
        return stable_id

    # Priority 4: Random
    fallback_id = str(uuid.uuid4())
    logger.debug(f"⚠️ Fallback to random UUID: {fallback_id[:8]}...")
    return fallback_id

def _build_openai_completion(content: str, model: str, chat_id: Optional[str], parent_id: Optional[str], usage: Optional[Dict[str, Any]] = None):
    """
    Build OpenAI-compatible completion response.
    Formats the response from Qwen API into the structure expected by
    OpenAI-compatible clients (OpenWebUI, LiteLLM, etc.).
    Args:
        content: Generated text content from Qwen
        model: Model name used for generation
        chat_id: Qwen chat ID (included in response for client reference)
        parent_id: Parent message ID (included in response for threading)
        usage: Optional token usage statistics
    Returns:
        Dict: OpenAI-compatible completion response dictionary
    """
    return {
        "id": f"chatcmpl-{uuid.uuid4()}", "object": "chat.completion", "created": int(time.time()),
        "model": model, "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "chatId": chat_id, "parentId": parent_id
    }

def _parse_qwen_error_json(parsed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Parse Qwen API error response into standardized format.
    Args:
        parsed: Parsed JSON response from Qwen API
    Returns:
        Dict|None: Standardized error dict with status/error/details, or None if not an error
    """
    top_code = parsed.get("code")
    nested_data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
    nested_code = nested_data.get("code")
    # Check multiple possible error indicators
    has_error = (parsed.get("success") is False or bool(parsed.get("error")) or bool(nested_data.get("error")) or bool(top_code) or bool(nested_code))
    if not has_error:
        return None
    # Special handling for rate limiting
    is_rate_limited = top_code == "RateLimited" or nested_code == "RateLimited"
    return {"status": 429 if is_rate_limited else 500, "error": "API Error", "details": json.dumps(parsed, ensure_ascii=False)}

def _format_user_error(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Formats error response for the user based on Qwen API result.

    Parses raw error/details, classifies the error type, and returns
    a structured message using Config.ERROR_MESSAGES for localization.

    Args:
        result: Dictionary containing execution result keys:
            - success: bool
            - status: int (HTTP status code)
            - error: str (error label)
            - details: str (raw error body, may be JSON string or plain text)

    Returns:
        Dict with formatted error information:
            - message: User-readable message with TTS language tags (from config)
            - type: Error classification (e.g., rate_limit_exceeded, chat_locked)
            - hint: Actionable recommendation for the user (from config)
            - status: HTTP status code to return to client
    """
    # Normalize status code
    status = result.get("status") or 500
    if not isinstance(status, int) or status < 400:
        status = 500
    raw_error = result.get("error", "")
    raw_details = result.get("details", "")

    # Combine all text data for pattern matching — always use lower() and strip()
    combined = f"{raw_error} {raw_details}".lower().strip()

    # Attempt to parse JSON from details field (may fail — that's OK)
    parsed_json = None
    if raw_details:
        try:
            parsed_json = json.loads(raw_details)
        except (json.JSONDecodeError, TypeError):
            pass

    # === ERROR CLASSIFICATION (ORDER IS CRITICAL!) ===
    error_type = "unknown_error"
    detail_msg = ""

    # 1. Rate Limited
    if status == 429 or "ratelimited" in combined or "rate limit" in combined:
        error_type = "rate_limit_exceeded"
        status = 429

    # 2. Chat Locked / In Progress
    elif "chat is in progress" in combined or "chat locked" in combined:
        error_type = "chat_locked"
        status = 409

    # 3. Auth Failed / Unauthorized
    elif status == 401 or "unauthorized" in combined or "invalid token" in combined or "authentication" in combined:
        error_type = "auth_failed"
        status = 401

    # 4. Model Overloaded / Busy
    elif "model overloaded" in combined or "service busy" in combined:
        error_type = "model_overloaded"
        status = 503

    # 5. 🚨 CONTEXT LENGTH EXCEEDED - now works REGARDLESS of status!
    # We check using keywords and regular expressions in raw_details (even if the status is 502/500)
    context_len_patterns = [
        "input length",
        "range of input",
        "max input length",
        "exceeds max length",
        "invalidparameter",
        "algo.invalidparameter"
    ]
    if any(p in combined for p in context_len_patterns):
        # We are looking for numbers in brackets: [1, 258048]
        import re
        match = re.search(r"\[(\d+)\s*,\s*(\d+)\]", raw_details)
        if not match:
            # We're trying to find just the numbers after "should be" or "max"
            match = re.search(r"should be.*?(\d+)", raw_details)
            if not match:
                match = re.search(r"max.*?(\d+)", raw_details)
        if match:
            max_len = int(match.group(2) if len(match.groups()) > 1 else match.group(1))
            detail_msg = f" {max_len}"
        else:
            detail_msg = " 258048"  # fallback default

        error_type = "context_length_exceeded"
        status = 400  # always 400 for this error, even if Qwen returned 502

    # 6. Invalid Request / Bad Parameters
    elif status == 400 or "invalid request" in combined or "bad request" in combined:
        error_type = "invalid_request"
        status = 400
        if parsed_json and isinstance(parsed_json, dict):
            detail_msg = parsed_json.get("message", "") or parsed_json.get("data", {}).get("details", "")

    # 7. Timeout / Connection Issues
    elif "timeout" in combined:
        error_type = "network_timeout"
        status = 504

    # 8. General Internal Server Error - ONLY if nothing above worked
    elif status >= 500 or "internal server error" in combined or "internal error" in combined:
        error_type = "upstream_internal_error"
        status = 502

    # === RETRIEVE MESSAGE FROM CONFIG ===
    cfg = Config.ERROR_MESSAGES.get(error_type, Config.ERROR_MESSAGES["unknown_error"])
    # Build final message
    message = cfg["message"]
    if "{max_len}" in message:
        max_val = detail_msg.strip() or "258048"
        message = message.format(max_len=max_val)
    elif detail_msg:
        message = f"{message} {detail_msg}"

    return {
        "message": message,
        "type": error_type,
        "hint": cfg["hint"],
        "status": status
    }

async def execute_qwen_completion(token_obj, chat_id, payload, on_chunk=None, is_new_chat: bool = False, request_timeout: Optional[float] = None):
    """
    Execute completion request to Qwen API with optimized retry logic.
    Handles:
    - Streaming and non-streaming responses
    - Error parsing and retry decisions
    - "Chat in progress" transient errors with exponential backoff
    - Token usage tracking and response ID extraction
    Args:
        token_obj: Authentication token dictionary
        chat_id: Qwen chat ID to send request to
        payload: Request payload dictionary
        on_chunk: Optional callback(chunk_text) for streaming responses
        is_new_chat: Flag indicating if this is a newly created chat (affects retry behavior)
        request_timeout: Optional custom timeout in seconds
    Returns:
        Dict: Result dictionary with keys:
            - success: bool
            - content: str (generated text)
            - response_id: str|None (Qwen response ID for threading)
            - usage: dict|None (token counts)
            - status: int|None (HTTP status if error)
            - error: str|None (error message if failed)
            - details: str|None (raw error body if failed)
    """
    token = token_obj["token"]
    # Set cookies from token_obj for session persistence
    if "cookies" in token_obj:
        for cookie in token_obj["cookies"]:
            if cookie["name"] not in http_client.cookies:
                http_client.cookies.set(cookie["name"], cookie['value'], domain=cookie['domain'])

    # Build request headers for Qwen API
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "Accept": "*/*",
        "User-Agent": Config.DEFAULT_HEADERS["User-Agent"],
        "Accept-Language": Config.DEFAULT_HEADERS["Accept-Language"],
        "Origin": Config.QWEN_BASE_URL,
        "Referer": f"{Config.QWEN_BASE_URL}/c/{chat_id}",
    }

    # Use custom timeout if provided, else default from Config
    timeout = request_timeout if request_timeout else Config.HTTP_TIMEOUT
    logger.debug(f"Sending request to chat {chat_id}... (timeout={timeout}s, is_new_chat={is_new_chat})")

    # Configure retry strategy: more retries for new chats (more likely to have init issues)
    base_max_retries = 2 if is_new_chat else 1
    start_time = time.time()  # Track total time for logging

    # =================================================================
    # 🔥 OUTER LOOP: For fast network errors (Connection Reset, 502, 503)
    # =================================================================
    for attempt in range(base_max_retries + 1):
        try:
            async with http_client.stream(
                "POST",
                f"{Config.CHAT_API_URL}?chat_id={chat_id}",
                headers=headers,
                json=payload,
                timeout=timeout
            ) as response:
                # Qwen API uses x-actual-status-code header for logical status
                # (HTTP 200 for streaming even if logical error)
                actual_status_raw = response.headers.get("x-actual-status-code")
                actual_status = int(actual_status_raw) if actual_status_raw else None

                if actual_status and actual_status >= 400:
                    logger.warning(f"Qwen returned x-actual-status-code: {actual_status}")
                    # Read response body for error details
                    body = (await response.aread()).decode("utf-8", errors="ignore")
                    logger.error(f"❌ HTTP {response.status_code} from Qwen (actual: {actual_status}): {body[:500]}")

                    # Parse error details for retry decisions
                    error_details = ""
                    try:
                        err_json = json.loads(body)
                        error_details = err_json.get("data", {}).get("details", "").lower()
                        logger.error(f"❌ Qwen 400 error details: {json.dumps(err_json, ensure_ascii=False)}")
                    except:
                        logger.error(f"❌ Qwen 400 error body (raw): {body[:300]}")

                    # =================================================================
                    # 🔥 SPECIAL PROCESSING: "The chat is in progress!" (Session lock)
                    # This is NOT a network error, it is a "WAIT" signal.
                    # We start an INDEPENDENT internal ping loop.
                    # =================================================================
                    is_chat_in_progress = "chat is in progress" in error_details

                    if is_chat_in_progress:
                        logger.warning(f"🔒 Chat locked! Starting independent wait loop (ping)...")

                        # 🔥 INNER LOOP: 6 attempts to wait, regardless of base_max_retries
                        # This is the "chat ping".
                        for lock_attempt in range(6):
                            if lock_attempt > 0:
                                # Exponential backoff for "ping"
                                if lock_attempt == 1: delay = 30.0
                                elif lock_attempt == 2: delay = 45.0
                                elif lock_attempt == 3: delay = 60.0
                                elif lock_attempt == 4: delay = 90.0
                                elif lock_attempt == 5: delay = 120.0
                                else: delay = 180.0

                                logger.warning(f"⏳ Waiting {delay}s before ping retry {lock_attempt+1}/6...")
                                await asyncio.sleep(delay)

                            # REQUEST AGAINST LOOP
                            try:
                                async with http_client.stream(
                                    "POST",
                                    f"{Config.CHAT_API_URL}?chat_id={chat_id}",
                                    headers=headers,
                                    json=payload,
                                    timeout=timeout
                                ) as retry_response:
                                    retry_status_raw = retry_response.headers.get("x-actual-status-code")
                                    retry_status = int(retry_status_raw) if retry_status_raw else None

                                    if retry_status and retry_status >= 400:
                                        retry_body = (await retry_response.aread()).decode("utf-8", errors="ignore")
                                        retry_err = ""
                                        try:
                                            retry_err = json.loads(retry_body).get("data", {}).get("details", "").lower()
                                        except: pass

                                        if "chat is in progress" in retry_err:
                                            logger.warning(f"🔒 Still locked. Attempt {lock_attempt+1}/6 failed.")
                                            continue # Let's go to the next break
                                        else:
                                            # Another error - we break the lock cycle and return an error
                                            logger.error(f"❌ New error during lock wait: {retry_status}")
                                            return {"success": False, "status": retry_status, "error": "API Error", "details": retry_body}
                                    else:
                                        # Success! The chat is free.
                                        logger.info(f"✅ Chat unlocked after {lock_attempt+1} waits!")
                                        # We transfer control to processing the successful response stream.
                                        return await _process_stream_response(retry_response, chat_id, start_time, on_chunk)
                            except Exception as e:
                                logger.error(f"Error during lock retry: {e}")
                                continue

                        # If the cycle ends and the chat is still busy
                        elapsed = time.time() - start_time
                        logger.error(f"❌ Chat still in progress after 6 pings ({elapsed/60:.1f} min)")
                        return {"success": False, "status": actual_status, "error": "Chat locked after max retries", "details": body}

                    # Standard 400/500 errors (not chat blocking)
                    if actual_status in (400, 500) and attempt < base_max_retries:
                        retry_delay = 1.0 if is_new_chat else 0.5
                        logger.warning(f"🔁 Retry {attempt+1}/{base_max_retries} (standard error, delay={retry_delay}s)")
                        await asyncio.sleep(retry_delay)
                        continue

                    elapsed = time.time() - start_time
                    logger.error(f"❌ Failed after {elapsed:.1f}s: chat {chat_id[:8]}... returned {actual_status}")
                    return {"success": False, "status": actual_status, "error": "API Error", "details": body}

                # If the status is OK (200), we process the stream.
                if response.status_code != 200:
                    body = (await response.aread()).decode("utf-8", errors="ignore")
                    logger.error(f"❌ HTTP {response.status_code} from Qwen: {body[:500]}")
                    return {"success": False, "status": response.status_code, "error": "API Error", "details": body}

                # Processing SSE stream
                return await _process_stream_response(response, chat_id, start_time, on_chunk)

        except Exception as e:
            logger.error(f"Error requesting Qwen API (attempt {attempt+1}): {e}")
            if attempt < base_max_retries:
                retry_delay = 1.0 if is_new_chat else 0.5
                logger.warning(f"🔁 Retry {attempt+1}/{base_max_retries} for chat {chat_id[:8]}... (exception: {e}, delay={retry_delay}s)")
                await asyncio.sleep(retry_delay)
                continue
            return {"success": False, "status": 500, "error": "Proxy error", "details": str(e)}

    # All attempts have been exhausted
    elapsed = time.time() - start_time
    logger.error(f"❌ Max retries exceeded after {elapsed:.1f}s ({elapsed/60:.1f} min) for chat {chat_id[:8]}...")
    return {
        "success": False,
        "status": 500,
        "error": "Max retries exceeded",
        "details": "Failed after multiple attempts"
    }

# ==================================================================
# 🔥 HELPER: Stream processing has been moved to a separate function
# To avoid duplicating a huge chunk of code inside a nested loop
# ==================================================================
async def _process_stream_response(response, chat_id, start_time, on_chunk):
    """
    Helper function to process SSE stream from Qwen API.
    Used by both main request and retry loops.
    
    🔥 UPDATED: Errors are now handled via _format_user_error
    for correct classification (including context_length_exceeded) and hints.
    """
    full_content = ""
    response_id = None
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    
    async for raw_line in response.aiter_lines():
        line = raw_line.strip()
        # Skip empty lines and non-data lines
        if not line or not line.startswith("data:"):
            continue
            
        data_str = line[5:].strip()  # Remove "data: " prefix
        if not data_str or data_str == "[DONE]":
            break
            
        try:
            chunk = json.loads(data_str)
        except Exception:
            continue

        # =================================================================
        # 🔥 Error Handling via _format_user_error
        # =================================================================
        
        # 1. Rate limiting errors within stream
        if chunk.get("code") == "RateLimited" or (chunk.get("code") and chunk.get("detail")):
            # We format through the errors processor
            formatted = _format_user_error({
                "success": False,
                "status": 429,
                "error": "RateLimited",
                "details": json.dumps(chunk, ensure_ascii=False)
            })
            return {
                "success": False,
                "status": formatted["status"],
                "error": formatted["type"],       # For example: rate_limit_exceeded
                "message": formatted["message"],  # Message text
                "hint": formatted.get("hint", ""),# Hint
                "details": json.dumps(chunk, ensure_ascii=False)
            }

        # 2. Generic errors within stream
        if chunk.get("error") and not chunk.get("choices"):
            # Extract details from the nested error structure, if any.
            nested_error = chunk.get("error", {})
            raw_details = json.dumps(nested_error, ensure_ascii=False) if isinstance(nested_error, dict) else json.dumps(chunk, ensure_ascii=False)
            
            # We format through the errors processor
            formatted = _format_user_error({
                "success": False,
                "status": 500,
                "error": "API Error",
                "details": raw_details
            })
            return {
                "success": False,
                "status": formatted["status"],
                "error": formatted["type"],       # For example: context_length_exceeded
                "message": formatted["message"],  # Message text
                "hint": formatted.get("hint", ""),# Hint
                "details": raw_details
            }

        # =================================================================
        # PROCESSING SUCCESSFUL CHUNKS
        # =================================================================
        
        # Extract response_id from metadata if present
        if chunk.get("response_id"):
            response_id = chunk["response_id"]
            
        # Track token usage if provided
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
            
        # Extract content from choices
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
            
        first_choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = first_choice.get("delta") if isinstance(first_choice.get("delta"), dict) else {}
        piece = delta.get("content")
        
        if piece is not None:
            piece_str = str(piece)
            full_content += piece_str
            # Call streaming callback if provided
            if callable(on_chunk):
                on_chunk(piece_str)
                
        # Check for stream completion
        if delta.get("status") == "finished" or first_choice.get("finish_reason"):
            break

    # Log success
    if start_time:
        elapsed = time.time() - start_time
        if elapsed > 1.0:
             logger.info(f"✅ Success ({elapsed:.1f}s) for chat {chat_id[:8]}...")
             
    return {
        "success": True,
        "content": full_content,
        "response_id": response_id,
        "usage": usage,
        "finished": True
    }

# =================================================================
# FASTAPI APP with LIFESPAN
# =================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan handler: manage startup/shutdown lifecycle.
    - Startup: Initialize chat state backend + asyncpg DB pool
    - Shutdown: Close HTTP client + DB pools
    Args:
        app: FastAPI application instance
    """
    logger.info("FastAPI startup: initializing chat state backend...")
    # 🔥 Init backend (handles File/Postgres/Fallback)
    await init_chat_state()

    # Show active mode
    if is_fallback_active():
        logger.warning("⚠️ ChatState is running in FALLBACK mode (File). PostgreSQL was unavailable.")
    else:
        logger.info(f"✅ ChatState backend initialized: {Config.CHAT_STATE_BACKEND.value}")

    # Initializing OpenWebUI DB (lookup only)
    logger.info("FastAPI startup: initializing asyncpg pool...")
    await init_db_pool()

    yield

    logger.info("FastAPI shutdown: cleaning up resources...")
    await http_client.aclose()

    # 🔥 Close backends
    await close_chat_state()   # Closes the State DB pool
    await close_db_pool()      # Closes the OpenWebUI DB pool

# Create FastAPI application with lifespan management
app = FastAPI(title="FreeQwenApi Python", lifespan=lifespan)

# Add CORS middleware to allow cross-origin requests (for web clients)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def _stream_openai_response(token_info, chat_id: str, payload: Dict[str, Any], model: str, openweb_chat_id: str):
    """
    Streaming response generator in OpenAI-compatible SSE format.
    Implements Server-Sent Events (SSE) protocol for streaming token-by-token responses:
    - Each chunk: `{"id":...,"object":"chat.completion.chunk",...}\n\n`
    - Final chunk: `[DONE]\n\n`
    Args:
        token_info: Authentication token dictionary
        chat_id: Qwen chat ID
        payload: Request payload for Qwen API
        model: Model name for response metadata
        openweb_chat_id: OpenWebUI chat ID for state updates
    Yields:
        str: SSE-formatted chunks for StreamingResponse
    """
    queue: asyncio.Queue = asyncio.Queue()
    has_streamed_chunks = False
    last_activity = time.time()
    PING_INTERVAL = 15  # Send ping every 15 seconds of inactivity
    def on_chunk(chunk_text: str):
        """Callback: called by execute_qwen_completion for each generated token"""
        if chunk_text:
            queue.put_nowait(chunk_text)
    # Start Qwen API request as background task
    task = asyncio.create_task(execute_qwen_completion(token_info, chat_id, payload, on_chunk=on_chunk))
    logger.info(f"📡 Stream started for chat {chat_id[:8]}... (model={model})")
    try:
        while True:
            # 🔥 Check if background task completed
            if task.done():
                logger.debug(f"📡 Task done for chat {chat_id[:8]}..., draining queue...")
                # Drain any remaining chunks from queue
                while not queue.empty():
                    try:
                        chunk = queue.get_nowait()
                        has_streamed_chunks = True
                        last_activity = time.time()
                        # ✅ CORRECT SSE FORMAT: "data: " + JSON + "\n\n"
                        yield "data: " + json.dumps({
                            "id": "chatcmpl-stream",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}]
                        }, ensure_ascii=False) + "\n\n"
                        logger.debug(f"📡 Drained chunk ({len(chunk)} chars) for chat {chat_id[:8]}...")
                    except asyncio.QueueEmpty:
                        break
                # If task failed and we haven't streamed anything, send error chunk
                if not has_streamed_chunks:
                    try:
                        result = task.result()
                        if not result.get("success"):
                            formatted = _format_user_error(result)
                
                            # ✅ Forming the text: Message + Hint (if any)
                            # All this will be treated as regular content, and the interface will display it as bot text, properly TTS talk.
                            full_text = formatted["message"]
                            if formatted.get("hint"):
                                full_text += f"\n\n{formatted['hint']}"
                
                            logger.warning(f"📡 Sending error as text: {full_text[:100]}...")
                
                            # ✅ We ship STRICTLY via delta.content
                            yield "data: " + json.dumps({
                                "id": "chatcmpl-stream",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": model,
                                "choices": [{"index": 0, "delta": {"content": full_text}, "finish_reason": None}]
                            }, ensure_ascii=False) + "\n\n"
                
                    except Exception as e:
                        logger.error(f"📡 Error getting task result: {e}")
                        # Fallback is also strictly like text.
                        fallback_msg = Config.ERROR_MESSAGES.get("unknown_error", {}).get("message", "Check API Engine!")
                        yield "data: " + json.dumps({
                            "id": "chatcmpl-stream",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [{"index": 0, "delta": {"content": fallback_msg}, "finish_reason": None}]
                        }, ensure_ascii=False) + "\n\n"
                break
            # 🔥 Send ping chunk if no activity for PING_INTERVAL seconds
            if time.time() - last_activity > PING_INTERVAL:
                # Send empty comment line to keep connection alive (SSE spec allows this)
                yield ": ping\n\n"
                last_activity = time.time()
                logger.debug(f"📡 Sent ping for chat {chat_id[:8]}... (idle > {PING_INTERVAL}s)")
                await asyncio.sleep(0.1)  # Small delay to avoid tight loop
                continue
            # Normal case: wait for next chunk from queue
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            has_streamed_chunks = True
            last_activity = time.time()
            # ✅ CORRECT SSE FORMAT
            yield "data: " + json.dumps({
                "id": "chatcmpl-stream",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}]
            }, ensure_ascii=False) + "\n\n"
            logger.debug(f"📡 Streamed chunk ({len(chunk)} chars) for chat {chat_id[:8]}...")
        # 🔥 If task succeeded but we haven't streamed (non-streaming response), send full content
        if task.done() and not has_streamed_chunks:
            try:
                result = task.result()
                if result.get("success") and result.get("content"):
                    content = result["content"]
                    logger.info(f"📡 Sending full content ({len(content)} chars) as single chunk for chat {chat_id[:8]}...")
                    yield "data: " + json.dumps({
                        "id": "chatcmpl-stream",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]
                    }, ensure_ascii=False) + "\n\n"
            except Exception as e:
                logger.error(f"📡 Error sending full content: {e}")
        # Extract response_id for state update
        response_id = None
        if task.done():
            try:
                result = task.result()
                response_id = result.get("response_id")
            except:
                pass
        # Update parent_id mapping for next message in conversation
        if response_id and openweb_chat_id:
            await update_chat_parent_id(openweb_chat_id, response_id)
            logger.debug(f"📡 Updated last_parent_id for {openweb_chat_id[:8]}...: {response_id[:8]}...")
        # 🔥 FINAL CHUNKS: Also use correct SSE format
        logger.debug(f"📡 Sending final chunk for chat {chat_id[:8]}...")
        yield "data: " + json.dumps({
            "id": "chatcmpl-stream",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        }, ensure_ascii=False) + "\n\n"
        # 🔥 [DONE] marker with correct prefix
        yield "data: [DONE]\n\n"
        logger.info(f"📡 Stream completed for chat {chat_id[:8]}... (sent={has_streamed_chunks})")
    except GeneratorExit:
        logger.warning(f"📡 Stream cancelled for chat {chat_id[:8]}... (client disconnected?)")
        raise
    except Exception as e:
        logger.error(f"📡 Stream error for chat {chat_id[:8]}...: {e}", exc_info=True)
        raise
    finally:
        # Cleanup: cancel task if still running
        if not task.done():
            logger.debug(f"📡 Cancelling task for chat {chat_id[:8]}...")
            task.cancel()

async def handle_chat_completions(request: Request, body: Dict[str, Any]):
    """
    Main handler for chat completion requests.
    Orchestrates the full request flow:
    1. Parse and validate request
    2. Get authentication token
    3. Determine chat ID (explicit, DB, or generated)
    4. Get or create Qwen chat
    5. Build and send request to Qwen API
    6. Return streaming or non-streaming response
    Args:
        request: FastAPI Request object
        body: Parsed JSON request body
    Returns:
        StreamingResponse|JSONResponse: OpenAI-compatible response
    """
    # Extract messages from request body
    messages = _extract_messages(body)
    if not messages:
        return JSONResponse(status_code=400, content={"error": "Messages not specified"})
    # Extract and map model name
    model = body.get("model", Config.DEFAULT_MODEL)
    stream = bool(body.get("stream", False))
    mapped_model = get_mapped_model(model)
    # Get available authentication token
    token_info = get_available_token()
    if not token_info:
        return JSONResponse(status_code=401, content={"error": "No available accounts."})
    # Extract system message if present
    system_msg_obj = next((m for m in messages if isinstance(m, dict) and m.get("role") == "system"), None)
    system_msg = system_msg_obj.get("content") if isinstance(system_msg_obj, dict) else body.get("systemMessage")
    # Extract user message (last user message in conversation)
    user_msg_obj = next((m for m in reversed(messages) if isinstance(m, dict) and m.get("role") == "user"), None)
    if not user_msg_obj:
        return JSONResponse(status_code=400, content={"error": "No user messages in request"})
    # Normalize message content and extract files
    message_content = _normalize_message_content(user_msg_obj.get("content", ""))
    files = user_msg_obj.get("files") if isinstance(user_msg_obj.get("files"), list) else body.get("files") or []
    # Debug logging (only if enabled in Config)
    if Config.DEBUG_LOGGING:
        logger.debug(f"🔍 RAW BODY KEYS: {list(body.keys())}")
        logger.debug(f"🔍 HEADERS: {dict(request.headers)}")
    # Extract chat_id and parent_id from request
    extracted_chat_id, incoming_parent_id = _extract_chat_ids(body)

    # Determine final OpenWebUI chat ID using priority logic (ASYNC VERSION)
    if extracted_chat_id:
        openweb_chat_id = extracted_chat_id
    else:
        # Use the new async helper which includes DB lookup
        openweb_chat_id = await _generate_openweb_chat_id_async(request, body, model)

    if Config.DEBUG_LOGGING:
        logger.debug(f"🔍 Processing: openweb_chat_id={openweb_chat_id}, incoming_parent_id={incoming_parent_id}, model={mapped_model}")

    # Backend handles persistence automatically.

    # Get backend instance
    backend = get_chat_state_backend()

    # Check if chat exists via backend
    state = await backend.get(openweb_chat_id)
    is_new_chat = state is None or not state.qwen_chat_id

    # 🔥 Increase timeout for large messages in new chats
    request_timeout = Config.HTTP_TIMEOUT
    content_size = len(str(message_content)) if message_content else 0
    if is_new_chat and content_size > 5000:
        request_timeout = Config.HTTP_TIMEOUT * 2
        logger.debug(f"🔁 Extended timeout for new chat with large message: {request_timeout}s (content_size={content_size})")
    # Get or create Qwen chat for this OpenWebUI chat
    qwen_chat_id = await get_or_create_qwen_chat(token_info, openweb_chat_id, mapped_model)
    if not qwen_chat_id:
        return JSONResponse(status_code=500, content={"error": "Failed to get or create chat in Qwen"})

    # =================================================================
    # 🔥 FLEXIBLE parent_id handling (model-specific, configurable via config/.env)
    # =================================================================
    effective_parent_id = None

    if state and state.qwen_chat_id:
        # 🔥 Logic for selecting parent_id depending on the model (from Config)
        if mapped_model in Config.MODELS_REQUIRING_PARENT_ID:
            # These models require a parent_id to continue the conversation.
            effective_parent_id = state.last_parent_id
            if Config.DEBUG_LOGGING:
                logger.debug(f"📌 Model {mapped_model} REQUIRES parent_id: {effective_parent_id[:8] if effective_parent_id else None}")
        elif mapped_model in Config.MODELS_WORKING_WITHOUT_PARENT_ID:
            # These models build history inside chat_id automatically
            effective_parent_id = None
            if Config.DEBUG_LOGGING:
                logger.debug(f"📌 Model {mapped_model} works WITHOUT parent_id (auto-history)")
        else:
            # Unknown model: try with parent_id (safer default)
            effective_parent_id = state.last_parent_id
            if Config.DEBUG_LOGGING:
                logger.debug(f"📌 Model {mapped_model} UNKNOWN: trying WITH parent_id (safe default)")
    else:
        # New chat: always parent_id=None for first message
        effective_parent_id = None

    if Config.DEBUG_LOGGING:
        logger.debug(f"🎯 Final: model={mapped_model}, parent_id={effective_parent_id[:8] if effective_parent_id else None}, chat_id={qwen_chat_id[:8] if qwen_chat_id else None}")
    # =================================================================
    # Build final payload for Qwen API
    payload = build_qwen_payload(message_content, mapped_model, qwen_chat_id, parent_id=effective_parent_id, system_message=system_msg, files=files)
    # Return streaming or non-streaming response based on request
    if stream:
        headers = {
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 🔥 Important for nginx proxy buffering
            "Content-Type": "text/event-stream",
        }
        return StreamingResponse(
            _stream_openai_response(token_info, qwen_chat_id, payload, mapped_model, openweb_chat_id),
            media_type="text/event-stream",
            headers=headers
        )
    # Non-streaming: execute request and return full response
    result = await execute_qwen_completion(
        token_info,
        qwen_chat_id,
        payload,
        is_new_chat=is_new_chat,
        request_timeout=request_timeout
    )
    if not result.get("success"):
        # Use new error formatting function
        formatted = _format_user_error(result)
    
        # Log technical details for debugging (not exposed to user)
        logger.warning(
            f"⚠️ Error for chat {qwen_chat_id[:8] if qwen_chat_id else 'N/A'}: "
            f"type={formatted['type']}, status={formatted['status']}, "
            f"message={formatted['message']}, hint={formatted.get('hint', '')}"
        )
        if Config.DEBUG_LOGGING:
            logger.debug(f"🔍 Raw error details: {result.get('details', '')[:500]}")
    
        # Build response for user
        error_response = {
            "error": {
                "message": formatted["message"],
                "type": formatted["type"]
            }
        }
        if formatted.get("hint"):
            error_response["error"]["hint"] = formatted["hint"]
    
        return JSONResponse(status_code=formatted["status"], content=error_response)
    
    # Update parent_id mapping after successful response
    response_id = result.get("response_id")
    if response_id and openweb_chat_id:
        await update_chat_parent_id(openweb_chat_id, response_id)
        if Config.DEBUG_LOGGING:
            logger.debug(f"Updated last_parent_id for {openweb_chat_id[:8]}...: {response_id[:8]}...")

    # Build and return OpenAI-compatible response
    response_parent_id = response_id or incoming_parent_id
    return _build_openai_completion(result.get("content", ""), model, qwen_chat_id, response_parent_id, usage=result.get("usage"))

# =================================================================
# API ROUTES
# =================================================================
@app.get("/api/chat/completions")
async def chat_completions_get():
    """Handle GET requests to /api/chat/completions (not supported)"""
    return JSONResponse(status_code=405, content={"error": "Method not supported", "message": "Use POST /api/chat/completions"})

@app.get("/api/v1/chat/completions")
async def chat_completions_v1_get():
    """Handle GET requests to /api/v1/chat/completions (not supported)"""
    return JSONResponse(status_code=405, content={"error": "Method not supported", "message": "Use POST /api/v1/chat/completions"})

@app.post("/api/chat/completions")
async def chat_completions(request: Request):
    """Handle POST requests to /api/chat/completions (main endpoint)"""
    body = await request.json()
    return await handle_chat_completions(request, body)

@app.post("/api/v1/chat/completions")
async def chat_completions_v1(request: Request):
    """Handle POST requests to /api/v1/chat/completions (OpenAI-compatible)"""
    body = await request.json()
    return await handle_chat_completions(request, body)

@app.post("/api/chat")
async def chat_endpoint(request: Request):
    """Handle POST requests to /api/chat (alternative endpoint)"""
    body = await request.json()
    return await handle_chat_completions(request, body)

@app.get("/api/models")
async def list_models():
    """Return list of available models in OpenAI-compatible format"""
    models = load_available_models()
    return {"object": "list", "data": [{"id": m, "object": "model", "created": 0, "owned_by": "qwen", "permission": []} for m in models]}

# =================================================================
# CLI MENU & LAUNCHER
# =================================================================
def print_banner():
    """Print application banner for CLI menu"""
    print(r"""   Qwen API Proxy
""")

async def interactive_menu():
    """
    Interactive CLI menu for managing the proxy.
    Provides options to:
    1. Add new authentication accounts via browser login
    2. Start the FastAPI proxy server
    3. Manage token list and chat state cache
    """
    while True:
        os.system('clear' if os.name == 'posix' else 'cls')
        print_banner()
        tokens = load_tokens()
        print("\nAccount list:")
        if not tokens:
            print("  (empty)")
        else:
            for i, t in enumerate(tokens):
                is_limited = False
                if t.get('resetAt'):
                    is_limited = datetime.fromisoformat(t['resetAt'].replace('Z', '+00:00')).timestamp() > time.time()
                status = "Limited" if is_limited else "OK"
                print(f"  {i+1} | {t['id']} | {status}")
        print("\n=== Menu ===")
        print("1 - Add new account")
        print("2 - Re-login (not implemented)")
        print("3 - Start proxy")
        print("4 - Delete account")
        print("5 - Clear chat cache")
        print("0 - Exit")
        try:
            choice = input("\nYour choice (Enter = 3): ").strip()
        except EOFError:
            break
        if choice == "" or choice == "3":
            if not tokens:
                print("Error: Add at least one account first (item 1).")
                time.sleep(2)
                continue
            print(f"\nStarting server on {Config.HOST}:{Config.PORT}...")
            config = uvicorn.Config(app, host=Config.HOST, port=Config.PORT, log_level="info")
            server = uvicorn.Server(config)
            await server.serve()
            break
        elif choice == "1":
            print("\n--- Add account ---")
            print("1 - Manual browser login")
            print("2 - Auto login (Email + Password)")
            sub_choice = input("Choose method: ").strip()
            if sub_choice == "2":
                email = input("Email: ").strip()
                password = input("Password: ").strip()
                await login_interactive(email, password, headless=False)
            else:
                await login_interactive(headless=False)
        elif choice == "4":
            if not tokens:
                continue
            try:
                idx = int(input("Enter account number to delete: ")) - 1
                if 0 <= idx < len(tokens):
                    tokens.pop(idx)
                    save_tokens(tokens)
                    print("Account deleted.")
                    time.sleep(1)
            except ValueError:
                pass
        elif choice == "5":
            # Clear chat cache via backend
            try:
                backend = get_chat_state_backend()
                # Note: Backend might not support clear() method, so we just log
                logger.info("Chat cache clear requested. Backend state will be reset on restart.")
            except Exception as e:
                logger.warning(f"Could not access backend for clear: {e}")

            # Remove files
            if Config.CHAT_STATE_FILE.exists():
                Config.CHAT_STATE_FILE.unlink()
                logger.info(f"Deleted file {Config.CHAT_STATE_FILE}")
            if Config.CHAT_MAPPING_FILE.exists():
                Config.CHAT_MAPPING_FILE.unlink()
                logger.info(f"Deleted file {Config.CHAT_MAPPING_FILE}")

            print("Chat cache cleared.")
            time.sleep(1)

        elif choice == "0":
            break

def parse_args():
    """
    Parse command line arguments for CLI launcher.
    Supported arguments:
    --start-proxy   : Start FastAPI proxy immediately
    --login         : Start interactive Qwen auth via browser
    --clear-tokens  : Remove old/dead tokens before login
    --list-tokens   : List current tokens and exit
    --email         : Email for login (optional, with --login)
    --password      : Password for login (optional, with --login)
    --host          : Host for uvicorn (default: from Config)
    --port          : Port for uvicorn (default: from Config)
    --reload        : Enable uvicorn auto-reload (development)
    Returns:
        argparse.Namespace: Parsed arguments
    """
    parser = argparse.ArgumentParser(description="FreeQwenApi CLI Launcher")
    parser.add_argument("--start-proxy", action="store_true", help="Start FastAPI proxy immediately")
    parser.add_argument("--login", action="store_true", help="Start interactive Qwen auth via browser")
    parser.add_argument("--clear-tokens", action="store_true", help="Remove old/dead tokens before login")
    parser.add_argument("--list-tokens", action="store_true", help="List current tokens")
    parser.add_argument("--email", type=str, help="Email for login (optional)")
    parser.add_argument("--password", type=str, help="Password for login (optional)")
    parser.add_argument("--host", default=Config.HOST, help="Host for uvicorn")
    parser.add_argument("--port", default=Config.PORT, type=int, help="Port for uvicorn")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn reload")
    return parser.parse_args()

# =================================================================
# MODULE-LEVEL INIT
# =================================================================
if __name__ == "__main__":
    # Entry point when run as script
    args = parse_args()
    if args.login:
        import asyncio
        asyncio.run(login_interactive(email=args.email, password=args.password, clear_existing=args.clear_tokens))
    elif args.list_tokens:
        tokens = load_tokens()
        print(json.dumps(tokens, indent=2, ensure_ascii=False))
    elif args.start_proxy:
        logger.info(f"Starting FastAPI proxy on {args.host}:{args.port} ...")
        logger.info(f"Log level: {'DEBUG' if Config.DEBUG_LOGGING else 'INFO'} (DEBUG_LOGGING={Config.DEBUG_LOGGING})")
        logger.info(f"OpenWebUI DB: {'enabled' if Config.OPENWEBUI_DB_ENABLED else 'disabled'} (using asyncpg)")
        logger.info(f"Chat ID mode: {Config.OPENWEBUI_CHAT_ID_MODE}")
        # 🔥 IMPORTANT: module name is "qwenapi", not "main" for uvicorn
        uvicorn.run("qwenapi:app", host=args.host, port=args.port, reload=args.reload)
    else:
        print("No action specified. Use --help for usage.")
