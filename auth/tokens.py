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
MODULE: AUTH TOKENS
Token management, round-robin, rate limiting.
"""
import json
import time
import logging
import asyncio
import concurrent.futures
from datetime import datetime
from typing import List, Dict, Optional

from config import Config

from token_backends.factory import get_token_backend
from token_backends.base import TokenData

logger = logging.getLogger(__name__)

# Global pointer for round-robin token selection
_pointer = 0

def _run_async(coro):
    """Safe co-routine start from synchronous code"""
    try:
        loop = asyncio.get_running_loop()
        # Мы внутри цикла (FastAPI). Запускаем в отдельном потоке.
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, coro)
            return future.result()
    except RuntimeError:
        # No cycle (CLI/Tests). Can start directly.
        return asyncio.run(coro)


def load_tokens() -> List[Dict]:
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
    try:
        backend = get_token_backend()
        tokens = _run_async(backend.load_all())
        return [t.to_dict() for t in tokens]
    except Exception as e:
        logger.error(f"load_tokens error: {e}")
        return []


def save_tokens(tokens_list: List[Dict]):
    """
    Save authentication tokens to persistent storage.
    Args:
        tokens: List of token dictionaries to save
    Side effects:
        - Writes tokens to Config.TOKENS_FILE (overwrites existing)
        - Logs errors if save fails
    """
    try:
        backend = get_token_backend()
        tokens = [TokenData.from_dict(t) for t in tokens_list]
        _run_async(backend.save_all(tokens))
    except Exception as e:
        logger.error(f"save_tokens error: {e}")


def get_available_token() -> Optional[Dict]:
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
    now = time.time() * 1000
    valid = [t for t in tokens if not t.get('invalid') and (not t.get('resetAt') or datetime.fromisoformat(t['resetAt'].replace('Z', '+00:00')).timestamp() * 1000 <= now)]
    if not valid:
        return None
    token_obj = valid[_pointer % len(valid)]
    _pointer = (_pointer + 1) % len(valid)
    return token_obj


def mark_rate_limited(token_id: str, hours: int = 24):
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
            reset_time = datetime.fromtimestamp(time.time() + hours * 3600)
            t['resetAt'] = reset_time.isoformat() + "Z"
            break
    save_tokens(tokens)
