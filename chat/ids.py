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
MODULE: CHAT IDS
Chat ID extraction, generation, DB lookup.
"""
import hashlib
import time
import uuid
import logging
from typing import Dict, Any, Optional, Tuple

from fastapi import Request

from config import Config
from db_async import fetch_chat_id_from_db

logger = logging.getLogger(__name__)


def _extract_chat_ids(body: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
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
