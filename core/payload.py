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
MODULE: CORE PAYLOAD
Payload building and normalization.
"""
import uuid
import time
from typing import Dict, Any, List, Optional


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
