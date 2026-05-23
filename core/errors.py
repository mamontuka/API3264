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
MODULE: CORE ERRORS
Error parsing and formatting logic.
"""
import json
import re
from typing import Dict, Any, Optional

from config import Config


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

    # 🔧 CONDITIONAL HINT LOGIC
    # Return hint only if enabled in config, otherwise return empty string
    hint_value = cfg["hint"] if Config.ERROR_HINTS_ENABLED else ""

    return {
        "message": message,
        "type": error_type,
        "hint": hint_value,
        "status": status
    }
