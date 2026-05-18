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
MODULE: CORE ENGINE
Main execution logic, streaming, retries.
"""
import asyncio
import json
import time
import logging
from typing import Dict, Any, Optional, Callable

import httpx

from config import Config
from .errors import _format_user_error

logger = logging.getLogger(__name__)

# Global HTTP client - will be injected from main
http_client: Optional[httpx.AsyncClient] = None


def set_http_client(client: httpx.AsyncClient):
    """Inject http_client from main module."""
    global http_client
    http_client = client


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
