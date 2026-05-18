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
MODULE: CORE RESPONSE
Response building helpers.
"""
import uuid
import time
from typing import Dict, Any, Optional


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
