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
MODULE: CHAT MAPPING
Chat creation, mapping, parent_id updates.
"""
import asyncio
import time
import json
import logging
from typing import Optional

import httpx

from config import Config
from chat_state.factory import get_chat_state_backend
from chat_state.base import ChatStateData

logger = logging.getLogger(__name__)

# HTTP client will be injected from main
http_client = None


def set_http_client(client):
    """Inject http_client from main module."""
    global http_client
    http_client = client


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


async def get_or_create_qwen_chat(token_obj, openweb_chat_id: str, model: str):
    """
    Get existing Qwen chat ID or create a new one for the given OpenWebUI chat.
    This is the core function for maintaining conversation continuity:
    1. Check if we already have a Qwen chat ID for this OpenWebUI chat
    2. If not, create a new chat on Qwen side and store the mapping
    3. Return the Qwen chat ID for use in subsequent API calls
    4. Uses backend abstraction (File or PostgreSQL) for state persistence.
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
    Uses backend abstraction for state updates.
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
