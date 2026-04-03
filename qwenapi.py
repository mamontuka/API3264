# Copyright (C) 2026
#
# Authors:
#
# Oleh Mamont - https://github.com/mamontuka
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
# along with this program. If not, see <https://www.gnu.org>.
#
#

"""
FreeQwenApi - OpenAI-compatible proxy for Qwen Chat
Main application module
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
from config import Config, setup_logging, get_pg_connection, close_all_pg_connections

# =================================================================
# INITIALIZATION
# =================================================================
# Ensure directories exist
Config.ensure_dirs()

# Setup logging
logger = setup_logging()

# Create HTTP client
http_client = httpx.AsyncClient(
    timeout=Config.HTTP_TIMEOUT,
    follow_redirects=Config.HTTP_FOLLOW_REDIRECTS
)

# Global state
CHAT_STATE: Dict[str, Any] = {}
CHAT_MAPPING_LOCK = asyncio.Lock()

# =================================================================
# STATE MANAGEMENT
# =================================================================
def load_chat_state():
    """Load chat mapping state from file"""
    global CHAT_STATE
    logger.info(f"load_chat_state() START | SESSION_DIR={Config.SESSION_DIR}")

    if Config.CHAT_STATE_FILE.exists():
        try:
            logger.info(f"File found: {Config.CHAT_STATE_FILE}, size: {Config.CHAT_STATE_FILE.stat().st_size} bytes")
            with open(Config.CHAT_STATE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                CHAT_STATE.update(loaded)
            logger.info(f"Loaded {len(CHAT_STATE)} records from state")
            if CHAT_STATE:
                sample = list(CHAT_STATE.items())[:2]
                logger.info(f"Sample keys: {[(k[:8]+'...', v['qwen_chat_id'][:8]+'...') for k,v in sample]}")
            return True
        except Exception as e:
            logger.error(f"Error loading {Config.CHAT_STATE_FILE}: {type(e).__name__}: {e}", exc_info=True)
    else:
        logger.warning(f"File not found: {Config.CHAT_STATE_FILE}")

    # Fallback: old format
    if Config.CHAT_MAPPING_FILE.exists():
        try:
            with open(Config.CHAT_MAPPING_FILE, "r", encoding="utf-8") as f:
                old_mapping = json.load(f)
                for key, value in old_mapping.items():
                    if isinstance(value, str):
                        CHAT_STATE[key] = {"qwen_chat_id": value, "last_parent_id": None}
                    else:
                        CHAT_STATE[key] = value
            logger.info(f"Loaded and converted old format: {len(CHAT_STATE)} records")
            return True
        except Exception as e:
            logger.warning(f"Error loading {Config.CHAT_MAPPING_FILE}: {e}")

    logger.warning("State is EMPTY after load")
    return False


def save_chat_state():
    """Atomically save chat mapping state to file"""
    Config.ensure_dirs()
    try:
        temp_file = str(Config.CHAT_STATE_FILE) + ".tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(CHAT_STATE, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, Config.CHAT_STATE_FILE)
        logger.debug(f"Saved state: {len(CHAT_STATE)} chats in {Config.CHAT_STATE_FILE}")
    except Exception as e:
        logger.error(f"Error saving {Config.CHAT_STATE_FILE}: {e}")


# =================================================================
# CHAT MAPPING
# =================================================================
async def get_or_create_qwen_chat(token_obj, openweb_chat_id: str, model: str):
    """Get or create a Qwen chat, return the Qwen chat ID"""
    openweb_chat_id = str(openweb_chat_id).strip()

    async with CHAT_MAPPING_LOCK:
        if openweb_chat_id in CHAT_STATE:
            qwen_id = CHAT_STATE[openweb_chat_id].get("qwen_chat_id")
            if qwen_id:
                logger.debug(f"Found existing chat: {openweb_chat_id} -> {qwen_id}")
                return qwen_id

        logger.info(f"Creating new Qwen chat for {openweb_chat_id}, model: {model}")
        qwen_chat_id = await create_qwen_chat(token_obj, model)
        if not qwen_chat_id:
            logger.error(f"Failed to create chat for {openweb_chat_id}")
            return None

        CHAT_STATE[openweb_chat_id] = {
            "qwen_chat_id": qwen_chat_id,
            "last_parent_id": None
        }
        save_chat_state()
        logger.info(f"Created and saved chat: {openweb_chat_id} -> {qwen_chat_id}")

        # 🔥 FIX: Small delay to allow Qwen to fully initialize the new chat
        # This prevents 400 errors on the first message to a new chat
        await asyncio.sleep(2.0)

    return qwen_chat_id


def update_chat_parent_id(openweb_chat_id: str, new_parent_id: str):
    """Update last_parent_id for a chat after successful response"""
    if openweb_chat_id in CHAT_STATE:
        CHAT_STATE[openweb_chat_id]["last_parent_id"] = new_parent_id
        save_chat_state()
        logger.debug(f"Updated: last_parent_id[{openweb_chat_id[:8]}...] = {new_parent_id[:8]}...")


def get_mapped_model(model_name: str) -> str:
    """Get mapped model name or return original if not found"""
    return Config.MODEL_MAPPING.get(model_name.lower(), model_name)


def load_available_models() -> List[str]:
    """Load list of available models from file and config"""
    models = set(Config.MODEL_MAPPING.keys())
    models.add(Config.DEFAULT_MODEL)
    if Config.AVAILABLE_MODELS_FILE.exists():
        try:
            with open(Config.AVAILABLE_MODELS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    value = line.strip()
                    if value and not value.startswith("#"):
                        models.add(value)
        except Exception as e:
            logger.warning(f"Failed to load models from {Config.AVAILABLE_MODELS_FILE}: {e}")
    return sorted(models)


# =================================================================
# TOKEN MANAGEMENT
# =================================================================
def load_tokens():
    """Load tokens from file"""
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
    """Save tokens to file"""
    Config.ensure_dirs()
    try:
        with open(Config.TOKENS_FILE, "w", encoding="utf-8") as f:
            json.dump(tokens, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving {Config.TOKENS_FILE}: {e}")


_pointer = 0
def get_available_token():
    """Get next available token using round-robin"""
    global _pointer
    tokens = load_tokens()
    now = time.time() * 1000
    valid = [t for t in tokens if not t.get('invalid') and (not t.get('resetAt') or datetime.fromisoformat(t['resetAt'].replace('Z', '+00:00')).timestamp() * 1000 <= now)]
    if not valid:
        return None
    token_obj = valid[_pointer % len(valid)]
    _pointer = (_pointer + 1) % len(valid)
    return token_obj


def mark_rate_limited(token_id, hours=24):
    """Mark a token as rate-limited"""
    tokens = load_tokens()
    for t in tokens:
        if t['id'] == token_id:
            reset_time = datetime.fromtimestamp(time.time() + hours * 3600)
            t['resetAt'] = reset_time.isoformat() + "Z"
            break
    save_tokens(tokens)


# =================================================================
# AUTH & BROWSER
# =================================================================
async def login_interactive(email=None, password=None, headless=False):
    """Interactive browser login to obtain Qwen token"""
    logger.info("Starting browser for auth (headless=%s)...", headless)
    if not os.path.exists(Config.CHROME_USER_DATA):
        os.makedirs(Config.CHROME_USER_DATA, exist_ok=True)
    logger.info(f"Using browser profile: {Config.CHROME_USER_DATA}")

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=Config.CHROME_USER_DATA,
            headless=headless,
            viewport={"width": 1280, "height": 720}
        )
        page = await browser.new_page()
        auth_url = f"{Config.QWEN_BASE_URL}/auth?action=signin"
        await page.goto(auth_url)

        if email and password:
            try:
                await page.wait_for_selector('input[type="text"], input[type="email"], #username', timeout=15000)
                await page.fill('input[type="text"], input[type="email"], #username', email)
                await page.keyboard.press("Enter")
                await asyncio.sleep(3)
                await page.wait_for_selector('input[type="password"], #password', timeout=10000)
                await page.fill('input[type="password"], #password', password)
                await page.keyboard.press("Enter")
            except Exception as e:
                logger.warning(f"Auto-fill failed: {e}")

        print("\n" + "="*50 + "\n               AUTHORIZATION\n" + "="*50)
        print("1. Login to Qwen account in browser.\n2. Wait for chat interface.\n3. Press Enter here.")
        print("="*50 + "\n")
        input("Press Enter after successful login...")

        token = None
        try:
            token = await page.evaluate("localStorage.getItem('token')")
        except Exception as e:
            logger.error(f"Failed to get token: {e}")
            await browser.close()
            return
        if not token:
            logger.error("Token not found!")
            await browser.close()
            return

        cookies = await page.context.cookies()
        tokens = load_tokens()
        account_name = email or f"acc_{int(time.time() * 1000)}"
        tokens = [t for t in tokens if t['id'] != account_name]
        tokens.append({
            "id": account_name, "token": token, "cookies": cookies,
            "added_at": datetime.now().isoformat(), "invalid": False, "resetAt": None
        })
        save_tokens(tokens)
        logger.info(f"Account {account_name} added successfully!")
        await browser.close()


# =================================================================
# CORE PROXY ENGINE
# =================================================================
async def create_qwen_chat(token_obj, model=Config.DEFAULT_MODEL):
    """Create a new chat on Qwen side"""
    token = token_obj['token']
    if 'cookies' in token_obj:
        for cookie in token_obj['cookies']:
            if cookie['name'] not in http_client.cookies:
                http_client.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'])

    headers = {
        "Content-Type": "application/json", "Authorization": f"Bearer {token}",
        "Accept": "*/*", "User-Agent": Config.DEFAULT_HEADERS["User-Agent"],
        "Accept-Language": Config.DEFAULT_HEADERS["Accept-Language"],
        "Origin": Config.QWEN_BASE_URL, "Referer": Config.CHAT_PAGE_URL,
    }
    payload = {
        "title": "New Chat", "models": [model], "chat_mode": "normal",
        "chat_type": "t2t", "timestamp": int(time.time() * 1000)
    }
    try:
        resp = await http_client.post(Config.CREATE_CHAT_URL, headers=headers, json=payload, timeout=30.0)
        if resp.status_code == 200:
            content_type = resp.headers.get("content-type", "")
            if "application/json" not in content_type:
                logger.error(f"Unexpected content ({content_type}): {resp.text[:500]}")
                return None
            try:
                data = resp.json()
                chat_id = data.get('data', {}).get('id')
                if chat_id:
                    logger.info(f"Chat created on Qwen: {chat_id}")
                return chat_id
            except Exception as je:
                logger.error(f"JSON parse error: {je}. Body: {resp.text[:500]}")
        else:
            logger.error(f"Chat creation error: {resp.status_code}, body: {resp.text[:500]}")
            if resp.status_code >= 400:
                try:
                    err_data = resp.json()
                    logger.error(f"Error details: {json.dumps(err_data, ensure_ascii=False)[:300]}")
                except:
                    logger.error(f"Error body: {resp.text[:300]}")
    except Exception as e:
        logger.error(f"Exception creating chat: {e}")
    return None


def build_qwen_payload(message_content, model, chat_id, parent_id=None, system_message=None, files=None):
    """Build payload for Qwen API request"""
    user_msg_id = str(uuid.uuid4())
    assistant_msg_id = str(uuid.uuid4())
    new_message = {
        "fid": user_msg_id, "parentId": parent_id, "parent_id": parent_id,
        "role": "user", "content": message_content, "chat_type": "t2t",
        "sub_chat_type": "t2t", "timestamp": int(time.time()), "user_action": "chat",
        "models": [model], "files": files or [], "childrenIds": [assistant_msg_id],
        "extra": {"meta": {"subChatType": "t2t"}},
        "feature_config": {"thinking_enabled": False, "output_schema": "phase"}
    }
    payload = {
        "stream": True, "incremental_output": True, "chat_id": chat_id,
        "chat_mode": "normal", "messages": [new_message], "model": model,
        "parent_id": parent_id, "timestamp": int(time.time())
    }
    if system_message:
        payload["system_message"] = system_message
    return payload


def _normalize_message_content(content):
    """Normalize message content to Qwen format"""
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
    """Extract messages list from request body"""
    messages = body.get("messages")
    if isinstance(messages, list) and messages:
        return messages
    if body.get("message") is not None:
        return [{"role": "user", "content": body.get("message")}]
    return []


def _extract_chat_ids(body: Dict[str, Any]):
    """
    Extract chat_id and parent_id from request body.
    Supports multiple formats: OpenAI, OpenWebUI, LibreChat, etc.
    """
    # Check top-level fields
    chat_id = None
    for field in Config.get_chat_id_fields():
        if body.get(field):
            chat_id = body[field]
            break

    # Check nested fields
    if not chat_id:
        for parent_key, child_key in Config.get_nested_chat_id_paths():
            parent = body.get(parent_key)
            if isinstance(parent, dict) and parent.get(child_key):
                chat_id = parent[child_key]
                break

    # Check parent_id fields
    parent_id = None
    for field in ["parentId", "parent_id", "x_qwen_parent_id", "message_id"]:
        if body.get(field):
            parent_id = body[field]
            break

    # Check nested parent_id
    if not parent_id:
        for parent_key, child_key in Config.get_nested_chat_id_paths():
            parent = body.get(parent_key)
            if isinstance(parent, dict) and parent.get(child_key):
                parent_id = parent[child_key]
                break

    return chat_id, parent_id


def _get_openwebui_chat_id_from_db(user_id: str, conversation_title: Optional[str] = None) -> Optional[str]:
    """
    Get stable chat ID from OpenWebUI PostgreSQL database.

    ✅ CORRECTED: Table name is 'chat' (not 'conversation') based on actual OpenWebUI schema.
    Columns: id (text), user_id (text), title (text), updated_at (bigint), ...

    Returns chat ID if found, None otherwise.
    """
    if not Config.OPENWEBUI_DB_ENABLED:
        return None

    try:
        conn = get_pg_connection()
        if not conn:
            return None

        from psycopg2.extras import RealDictCursor
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # 🔥 CORRECT TABLE: 'chat' (verified from user's DB inspection)
            try:
                cur.execute("""
                    SELECT id, title, user_id, updated_at
                    FROM chat
                    WHERE user_id = %s
                    ORDER BY updated_at DESC
                    LIMIT 1
                """, (user_id,))
                result = cur.fetchone()
                if result:
                    logger.debug(f"🗄 Found chat in DB: id={result['id'][:8]}..., user_id={result['user_id'][:8]}..., updated_at={result['updated_at']}")
                    return str(result['id'])
            except Exception as e:
                logger.debug(f"Query to 'chat' table failed: {e}")
                # Try to rollback in case of transaction error
                try:
                    conn.rollback()
                except:
                    pass

            logger.debug(f"🗄 No chat found for user_id={user_id}")
            return None

    except Exception as e:
        logger.warning(f"⚠️ Error querying OpenWebUI DB: {e}")
        return None


def _generate_openweb_chat_id(request: Request, body: Dict[str, Any], model: str) -> str:
    """
    Generate/extract chat_id for OpenWebUI with priority:
    1. Explicit conversation_id/chat_id from body/headers ← NEW CHATS from OpenWebUI
    2. ID from OpenWebUI PostgreSQL DB (table 'chat') ← AUTO-BINDING
    3. STABLE hash based on user_id + model + hour ← CONTINUE DIALOGUE (DEFAULT)
    4. Fallback: random UUID (only if no user_id)
    """
    # 🔥 1. Check explicit fields in request (OpenWebUI should send conversation_id for new chats)
    for field in ["conversation_id", "conversationId", "chatId", "chat_id", "thread_id", "threadId"]:
        if body.get(field):
            logger.debug(f"🔍 Using explicit {field}: {body[field][:8]}...")
            return str(body[field])

    # Check headers
    for header in ["x-chat-id", "x-conversation-id", "openwebui-chat-id", "x-openwebui-chat-id"]:
        if request.headers.get(header):
            logger.debug(f"🔍 Using header {header}: {request.headers[header][:8]}...")
            return str(request.headers[header])

    # Check nested fields
    for parent_key, child_key in Config.get_nested_chat_id_paths():
        parent = body.get(parent_key)
        if isinstance(parent, dict) and parent.get(child_key):
            logger.debug(f"🔍 Using nested {parent_key}.{child_key}: {parent[child_key][:8]}...")
            return str(parent[child_key])

    # 🔥 2. Try to get ID from OpenWebUI DB (table 'chat')
    user_id = request.headers.get(Config.OPENWEBUI_USER_ID_HEADER)
    if user_id and Config.OPENWEBUI_DB_ENABLED:
        db_chat_id = _get_openwebui_chat_id_from_db(user_id, body.get("title"))
        if db_chat_id:
            logger.debug(f"🗄 Using chat_id from DB: {db_chat_id[:8]}...")
            return db_chat_id

    # 🔥 3. STABLE hash for dialogue continuation (DEFAULT BEHAVIOR)
    # This ensures that messages from the same user within an hour continue in the same chat
    if user_id:
        hour_bucket = int(time.time() // 3600)  # Group by hour
        stable_key = f"{user_id}:{model}:{hour_bucket}"
        stable_id = hashlib.sha256(stable_key.encode()).hexdigest()[:32]
        logger.debug(f"🔁 Using stable chat_id: {stable_id[:8]}... (user={user_id[:8]}, model={model}, hour={hour_bucket})")
        return stable_id

    # 🔥 4. Last fallback: random UUID (only if no user_id available)
    fallback_id = str(uuid.uuid4())
    logger.debug(f"⚠️ Fallback to random UUID: {fallback_id[:8]}... (no user_id)")
    return fallback_id


def _build_openai_completion(content: str, model: str, chat_id: Optional[str], parent_id: Optional[str], usage: Optional[Dict[str, Any]] = None):
    """Build OpenAI-compatible completion response"""
    return {
        "id": f"chatcmpl-{uuid.uuid4()}", "object": "chat.completion", "created": int(time.time()),
        "model": model, "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "chatId": chat_id, "parentId": parent_id
    }


def _parse_qwen_error_json(parsed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Parse Qwen API error response"""
    top_code = parsed.get("code")
    nested_data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
    nested_code = nested_data.get("code")
    has_error = (parsed.get("success") is False or bool(parsed.get("error")) or bool(nested_data.get("error")) or bool(top_code) or bool(nested_code))
    if not has_error:
        return None
    is_rate_limited = top_code == "RateLimited" or nested_code == "RateLimited"
    return {"status": 429 if is_rate_limited else 500, "error": "Qwen API Error", "details": json.dumps(parsed, ensure_ascii=False)}


async def execute_qwen_completion(token_obj, chat_id, payload, on_chunk=None):
    """Execute completion request to Qwen API"""
    token = token_obj["token"]
    if "cookies" in token_obj:
        for cookie in token_obj["cookies"]:
            if cookie["name"] not in http_client.cookies:
                http_client.cookies.set(cookie["name"], cookie["value"], domain=cookie['domain'])

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "Accept": "*/*",
        "User-Agent": Config.DEFAULT_HEADERS["User-Agent"],
        "Accept-Language": Config.DEFAULT_HEADERS["Accept-Language"],
        "Origin": Config.QWEN_BASE_URL,
        "Referer": f"{Config.QWEN_BASE_URL}/c/{chat_id}",
    }
    logger.debug(f"Sending request to chat {chat_id}...")

    try:
        async with http_client.stream(
            "POST",
            f"{Config.CHAT_API_URL}?chat_id={chat_id}",
            headers=headers,
            json=payload,
            timeout=Config.HTTP_TIMEOUT
        ) as response:
            actual_status_raw = response.headers.get("x-actual-status-code")
            actual_status = int(actual_status_raw) if actual_status_raw else None

            if actual_status and actual_status >= 400:
                logger.warning(f"Qwen returned x-actual-status-code: {actual_status}")

            if response.status_code != 200:
                body = (await response.aread()).decode("utf-8", errors="ignore")
                logger.error(f"❌ HTTP {response.status_code} from Qwen: {body[:500]}")
                # 🔥 Log detailed error for 400 responses
                if response.status_code == 400:
                    try:
                        err_json = json.loads(body)
                        logger.error(f"❌ Qwen 400 error details: {json.dumps(err_json, ensure_ascii=False)}")
                    except:
                        logger.error(f"❌ Qwen 400 error body (raw): {body[:300]}")
                return {
                    "success": False,
                    "status": response.status_code,
                    "error": "Qwen API Error",
                    "details": body
                }

            content_type = (response.headers.get("content-type") or "").lower()
            if "text/event-stream" not in content_type:
                body = (await response.aread()).decode("utf-8", errors="ignore")
                try:
                    parsed = json.loads(body)
                except Exception:
                    return {
                        "success": False,
                        "status": actual_status or 500,
                        "error": "Unexpected non-SSE 200 response",
                        "details": body
                    }
                structured_error = _parse_qwen_error_json(parsed)
                if structured_error:
                    if actual_status and actual_status >= 400:
                        structured_error["status"] = actual_status
                    structured_error["success"] = False
                    return structured_error
                content = ""
                choices = parsed.get("choices")
                if isinstance(choices, list) and choices:
                    first_choice = choices[0] if isinstance(choices[0], dict) else {}
                    msg = first_choice.get("message") if isinstance(first_choice.get("message"), dict) else {}
                    content = str(msg.get("content") or "")
                elif parsed.get("success") is True and isinstance(parsed.get("data"), dict):
                    content = str(parsed["data"].get("content") or "")
                if content and callable(on_chunk):
                    on_chunk(content)
                return {
                    "success": True,
                    "content": content,
                    "response_id": parsed.get("response_id") or parsed.get("id"),
                    "usage": parsed.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                }

            full_content = ""
            response_id = None
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            async for raw_line in response.aiter_lines():
                line = raw_line.strip()
                if not line or not line.startswith(""):
                    continue
                data_str = line[5:].strip()
                if not data_str or data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except Exception:
                    continue
                if chunk.get("code") == "RateLimited" or (chunk.get("code") and chunk.get("detail")):
                    return {
                        "success": False,
                        "status": 429,
                        "error": "RateLimited",
                        "details": json.dumps(chunk, ensure_ascii=False)
                    }
                if chunk.get("error") and not chunk.get("choices"):
                    return {
                        "success": False,
                        "status": 500,
                        "error": "Qwen API Error",
                        "details": json.dumps(chunk, ensure_ascii=False)
                    }
                created_meta = chunk.get("response.created")
                if isinstance(created_meta, dict) and created_meta.get("response_id"):
                    response_id = created_meta["response_id"]
                if chunk.get("response_id"):
                    response_id = chunk["response_id"]
                chunk_usage = chunk.get("usage")
                if isinstance(chunk_usage, dict):
                    usage = chunk_usage
                choices = chunk.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                first_choice = choices[0] if isinstance(choices[0], dict) else {}
                delta = first_choice.get("delta") if isinstance(first_choice.get("delta"), dict) else {}
                piece = delta.get("content")
                if piece is not None:
                    piece_str = str(piece)
                    full_content += piece_str
                    if callable(on_chunk):
                        on_chunk(piece_str)
                if delta.get("status") == "finished" or first_choice.get("finish_reason"):
                    break
            return {
                "success": True,
                "content": full_content,
                "response_id": response_id,
                "usage": usage,
                "finished": True
            }
    except Exception as e:
        logger.error(f"Error requesting Qwen API: {e}")
        return {
            "success": False,
            "status": 500,
            "error": "Proxy error",
            "details": str(e)
        }


# =================================================================
# FASTAPI APP with LIFESPAN
# =================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan handler: load state on startup, cleanup on shutdown"""
    logger.info("FastAPI startup: loading chat state...")
    load_chat_state()
    yield
    logger.info("FastAPI shutdown: cleaning up resources...")
    await http_client.aclose()
    close_all_pg_connections()

app = FastAPI(title="FreeQwenApi Python", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _stream_openai_response(token_info, chat_id: str, payload: Dict[str, Any], model: str, openweb_chat_id: str):
    """Streaming response in OpenAI-compatible SSE format"""
    queue: asyncio.Queue = asyncio.Queue()
    has_streamed_chunks = False

    def on_chunk(chunk_text: str):
        if chunk_text:
            queue.put_nowait(chunk_text)

    task = asyncio.create_task(execute_qwen_completion(token_info, chat_id, payload, on_chunk=on_chunk))

    try:
        while True:
            if task.done() and queue.empty():
                break
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue

            has_streamed_chunks = True
            # 🔥 FIX: Правильный SSE-формат: "data: " + JSON + "\n\n"
            yield "data: " + json.dumps({
                "id": "chatcmpl-stream",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}]
            }, ensure_ascii=False) + "\n\n"

        result = await task
        if not result.get("success"):
            if not has_streamed_chunks:
                err_text = f"Error: {result.get('error', 'Qwen API Error')}"
                yield "data: " + json.dumps({
                    "id": "chatcmpl-stream",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": err_text}, "finish_reason": None}]
                }, ensure_ascii=False) + "\n\n"
        elif not has_streamed_chunks and result.get("content"):
            yield "data: " + json.dumps({
                "id": "chatcmpl-stream",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"content": result["content"]}, "finish_reason": None}]
            }, ensure_ascii=False) + "\n\n"

        response_id = result.get("response_id")
        if response_id and openweb_chat_id:
            update_chat_parent_id(openweb_chat_id, response_id)
            logger.debug(f"Updated last_parent_id for {openweb_chat_id[:8]}...: {response_id[:8]}...")

        # 🔥 FIX: Правильный финальный чанк с "data: "
        yield "data: " + json.dumps({
            "id": "chatcmpl-stream",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        }, ensure_ascii=False) + "\n\n"

        # 🔥 FIX: [DONE] тоже с префиксом "data: "
        yield "data: [DONE]\n\n"

    finally:
        if not task.done():
            task.cancel()


async def handle_chat_completions(request: Request, body: Dict[str, Any]):
    """Main handler for chat completions"""
    messages = _extract_messages(body)
    if not messages:
        return JSONResponse(status_code=400, content={"error": "Messages not specified"})

    model = body.get("model", Config.DEFAULT_MODEL)
    stream = bool(body.get("stream", False))
    mapped_model = get_mapped_model(model)

    token_info = get_available_token()
    if not token_info:
        return JSONResponse(status_code=401, content={"error": "No available accounts."})

    system_msg_obj = next((m for m in messages if isinstance(m, dict) and m.get("role") == "system"), None)
    system_msg = system_msg_obj.get("content") if isinstance(system_msg_obj, dict) else body.get("systemMessage")

    user_msg_obj = next((m for m in reversed(messages) if isinstance(m, dict) and m.get("role") == "user"), None)
    if not user_msg_obj:
        return JSONResponse(status_code=400, content={"error": "No user messages in request"})

    message_content = _normalize_message_content(user_msg_obj.get("content", ""))
    files = user_msg_obj.get("files") if isinstance(user_msg_obj.get("files"), list) else body.get("files") or []

    # Debug logging (only if enabled)
    if Config.DEBUG_LOGGING:
        logger.debug(f"🔍 RAW BODY KEYS: {list(body.keys())}")
        logger.debug(f"🔍 HEADERS: {dict(request.headers)}")

    # Extract IDs with multi-format support
    extracted_chat_id, incoming_parent_id = _extract_chat_ids(body)

    # Final determination of openweb_chat_id
    openweb_chat_id = (
        extracted_chat_id or
        _generate_openweb_chat_id(request, body, model)
    )

    if Config.DEBUG_LOGGING:
        logger.debug(f"🔍 Processing: openweb_chat_id={openweb_chat_id}, incoming_parent_id={incoming_parent_id}, model={mapped_model}")

    # Lazy load if state not loaded at startup
    if openweb_chat_id not in CHAT_STATE and Config.CHAT_STATE_FILE.exists():
        logger.warning(f"Lazy load: {openweb_chat_id} not in memory, trying to load file")
        try:
            with open(Config.CHAT_STATE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                CHAT_STATE.update(loaded)
            logger.info(f"Lazy loaded: {len(CHAT_STATE)} records")
        except Exception as e:
            logger.error(f"Lazy load failed: {e}")

    qwen_chat_id = await get_or_create_qwen_chat(token_info, openweb_chat_id, mapped_model)
    if not qwen_chat_id:
        return JSONResponse(status_code=500, content={"error": "Failed to get or create chat in Qwen"})

    # =================================================================
    # 🔥 FIX FOR QWEN API V2: parent_id handling
    # =================================================================
    # Qwen API v2 requires parent_id=None for continuing dialogue
    # Otherwise returns 400 Bad Request
    effective_parent_id = None

    # Check if chat already exists in our state
    chat_exists = openweb_chat_id in CHAT_STATE

    if chat_exists:
        # ✅ Chat already exists → DO NOT send parent_id
        # Qwen builds linear history inside chat_id automatically
        effective_parent_id = None
        if Config.DEBUG_LOGGING:
            logger.debug(f"📌 Continuing chat {openweb_chat_id[:8]}... → parent_id=None")
    else:
        # ⚠️ New chat → can send parent_id if client explicitly provided it
        # But for compatibility, better to use None
        if incoming_parent_id and Config.OPENWEBUI_CHAT_ID_MODE == "per_request":
            effective_parent_id = incoming_parent_id
            if Config.DEBUG_LOGGING:
                logger.debug(f"📌 New chat with explicit parent_id: {effective_parent_id[:8]}...")
        else:
            effective_parent_id = None
            if Config.DEBUG_LOGGING:
                logger.debug(f"📌 New chat → parent_id=None")

    if Config.DEBUG_LOGGING:
        logger.debug(f"🎯 Final parent_id={effective_parent_id[:8] if effective_parent_id else None}... (chat_id={qwen_chat_id[:8] if qwen_chat_id else None}...)")
    # =================================================================

    payload = build_qwen_payload(message_content, mapped_model, qwen_chat_id, parent_id=effective_parent_id, system_message=system_msg, files=files)

    if stream:
        headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
        return StreamingResponse(_stream_openai_response(token_info, qwen_chat_id, payload, mapped_model, openweb_chat_id), media_type="text/event-stream", headers=headers)

    result = await execute_qwen_completion(token_info, qwen_chat_id, payload)
    if not result.get("success"):
        status = result.get("status") or 500
        if not isinstance(status, int) or status < 400:
            status = 500
        return JSONResponse(status_code=status, content={"error": {"message": result.get("details") or result.get("error") or "Qwen API Error", "type": "upstream_error"}})

    response_id = result.get("response_id")
    if response_id and openweb_chat_id:
        update_chat_parent_id(openweb_chat_id, response_id)
        if Config.DEBUG_LOGGING:
            logger.debug(f"Updated last_parent_id for {openweb_chat_id[:8]}...: {response_id[:8]}...")

    response_parent_id = response_id or incoming_parent_id
    return _build_openai_completion(result.get("content", ""), model, qwen_chat_id, response_parent_id, usage=result.get("usage"))


# =================================================================
# API ROUTES
# =================================================================
@app.get("/api/chat/completions")
async def chat_completions_get():
    return JSONResponse(status_code=405, content={"error": "Method not supported", "message": "Use POST /api/chat/completions"})

@app.get("/api/v1/chat/completions")
async def chat_completions_v1_get():
    return JSONResponse(status_code=405, content={"error": "Method not supported", "message": "Use POST /api/v1/chat/completions"})

@app.post("/api/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    return await handle_chat_completions(request, body)

@app.post("/api/v1/chat/completions")
async def chat_completions_v1(request: Request):
    body = await request.json()
    return await handle_chat_completions(request, body)

@app.post("/api/chat")
async def chat_endpoint(request: Request):
    body = await request.json()
    return await handle_chat_completions(request, body)

@app.get("/api/models")
async def list_models():
    models = load_available_models()
    return {"object": "list", "data": [{"id": m, "object": "model", "created": 0, "owned_by": "qwen", "permission": []} for m in models]}


# =================================================================
# CLI MENU & LAUNCHER
# =================================================================
def print_banner():
    print(r"""   Qwen API Proxy
""")


async def interactive_menu():
    """Interactive CLI menu"""
    load_chat_state()
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
            if Config.CHAT_STATE_FILE.exists():
                Config.CHAT_STATE_FILE.unlink()
                logger.info(f"Deleted file {Config.CHAT_STATE_FILE}")
            if Config.CHAT_MAPPING_FILE.exists():
                Config.CHAT_MAPPING_FILE.unlink()
                logger.info(f"Deleted file {Config.CHAT_MAPPING_FILE}")
            CHAT_STATE.clear()
            print("Chat cache cleared.")
            time.sleep(1)
        elif choice == "0":
            break


def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description="FreeQwenApi CLI Launcher")
    parser.add_argument("--start-proxy", action="store_true", help="Start FastAPI proxy immediately")
    parser.add_argument("--login", action="store_true", help="Start interactive Qwen auth via browser")
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
    args = parse_args()
    if args.login:
        import asyncio
        asyncio.run(login_interactive(email=args.email, password=args.password))
    elif args.list_tokens:
        tokens = load_tokens()
        print(json.dumps(tokens, indent=2, ensure_ascii=False))
    elif args.start_proxy:
        logger.info(f"Starting FastAPI proxy on {args.host}:{args.port} ...")
        logger.info(f"Log level: {'DEBUG' if Config.DEBUG_LOGGING else 'INFO'} (DEBUG_LOGGING={Config.DEBUG_LOGGING})")
        logger.info(f"OpenWebUI DB: {'enabled' if Config.OPENWEBUI_DB_ENABLED else 'disabled'}")
        logger.info(f"Chat ID mode: {Config.OPENWEBUI_CHAT_ID_MODE}")
        # 🔥 CORRECTED: module name is qwenapi, not main
        uvicorn.run("qwenapi:app", host=args.host, port=args.port, reload=args.reload)
    else:
        print("No action specified. Use --help for usage.")
