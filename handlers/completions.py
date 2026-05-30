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
MODULE: HANDLERS COMPLETIONS
Main request processing, streaming, response building.
"""
import asyncio
import json
import time
import logging
from typing import Dict, Any, List, Optional

from fastapi import Request
from fastapi.responses import StreamingResponse, JSONResponse

from config import Config
from core.errors import _format_user_error
from core.payload import build_qwen_payload, _normalize_message_content
from core.response import _build_openai_completion
from auth.tokens import get_available_token
from chat.models import get_mapped_model
from chat.ids import _extract_chat_ids, _generate_openweb_chat_id_async
from chat.mapping import get_or_create_qwen_chat, update_chat_parent_id
from core.engine import execute_qwen_completion

logger = logging.getLogger(__name__)


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


async def _stream_openai_response(token_info, chat_id: str, payload: Dict[str, Any], model: str, openweb_chat_id: str, mapped_model: str):
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
        mapped_model: Mapped model name for state isolation
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
        # 🔧 UPDATED: Pass mapped_model for model-isolated state
        if response_id and openweb_chat_id:
            if Config.PARENT_ID_UPDATE_DELAY > 0:
                await asyncio.sleep(Config.PARENT_ID_UPDATE_DELAY)
            await update_chat_parent_id(openweb_chat_id, response_id, model=mapped_model)
            logger.debug(f"📡 Updated last_parent_id for {openweb_chat_id[:8]}... (model={mapped_model}): {response_id[:8]}...")

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

    # ✅ CHECKING TOOL CALL
    # If the request body contains tool_calls, then OpenWebUI has executed the tool
    # and is sending the result/continuation. We're adding a pause to sync with Qwen.
    if body.get("tool_calls"):
        logger.debug("🔧 Detected tool_calls in request body, adding delay...")
        if Config.TOOL_CALL_SYNC_DELAY > 0:
            await asyncio.sleep(Config.TOOL_CALL_SYNC_DELAY)

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
    from chat_state.factory import get_chat_state_backend
    backend = get_chat_state_backend()

    # 🔧 UPDATED: Get state with model isolation
    state = await backend.get(openweb_chat_id, model=mapped_model)
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
        # 🔧 UPDATED: Pass mapped_model to streaming handler for state isolation
        return StreamingResponse(
            _stream_openai_response(token_info, qwen_chat_id, payload, mapped_model, openweb_chat_id, mapped_model),
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
    # 🔧 UPDATED: Pass mapped_model for model-isolated state update
    response_id = result.get("response_id")
    if response_id and openweb_chat_id:
        await update_chat_parent_id(openweb_chat_id, response_id, model=mapped_model)
        if Config.DEBUG_LOGGING:
            logger.debug(f"Updated last_parent_id for {openweb_chat_id[:8]}... (model={mapped_model}): {response_id[:8]}...")

    # Build and return OpenAI-compatible response
    response_parent_id = response_id or incoming_parent_id
    return _build_openai_completion(result.get("content", ""), model, qwen_chat_id, response_parent_id, usage=result.get("usage"))
    