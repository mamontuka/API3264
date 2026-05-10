"""
title: Image Edit Proxy
description: >
  Редактирование изображений через внешний прокси с унифицированными статусами.
  Версия 1.7: Улучшена диагностика отображения, детальные логи, безопасный fallback.
author: Oleh Mamont - https://github.com/mamontuka
version: 1.7
license: GPLv3
requirements: aiohttp
"""

import asyncio
import base64
import io
import json
import logging
import os
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional
import aiohttp
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OWUI_INTERNAL_BASE = os.environ.get("OWUI_INTERNAL_BASE", "http://127.0.0.1:8181")
PROXY_URL_DEFAULT = os.environ.get(
    "EDIT_PROXY_URL", "http://10.32.64.2:7264/v1/images/edits"
)


def _extract_image_urls_from_messages(messages: List[Dict[str, Any]]) -> List[str]:
    found: List[str] = []
    if not messages:
        return found
    for msg in reversed(messages):
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    obj = part.get("image_url") or {}
                    url = obj.get("url") or obj.get("src") or ""
                    if url:
                        found.append(url)
    return found


async def _bytes_from_url(
    session: aiohttp.ClientSession,
    url: str,
    owui_base: str,
    auth_token: Optional[str] = None,
) -> Optional[bytes]:
    if not url:
        return None

    if url.startswith("data:"):
        try:
            _, data = url.split(",", 1)
            return base64.b64decode(data)
        except Exception as exc:
            logger.warning(f"[BYTES] Base64 decode failed: {exc}")
            return None

    if url.startswith("/"):
        full_url = owui_base.rstrip("/") + url
        headers = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        try:
            async with session.get(full_url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    logger.info(f"[BYTES] Fetched internal image: {len(data)} bytes")
                    return data
                logger.warning(f"[BYTES] Internal fetch failed: HTTP {resp.status}")
        except Exception as exc:
            logger.warning(f"[BYTES] Internal fetch error: {exc}")
        return None

    if url.startswith("http://") or url.startswith("https://"):
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    logger.info(f"[BYTES] Fetched external image: {len(data)} bytes")
                    return data
                logger.warning(f"[BYTES] External fetch failed: HTTP {resp.status}")
        except Exception as exc:
            logger.warning(f"[BYTES] External fetch error: {exc}")
        return None

    logger.warning(f"[BYTES] Unknown URL format: {url[:80]}")
    return None


async def _send_to_proxy(
    session: aiohttp.ClientSession,
    proxy_url: str,
    prompt: str,
    image_bytes: bytes,
    user_id: Optional[str] = None,
    model: Optional[str] = None,
    timeout: int = 300,
) -> Optional[bytes]:
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "prompt": prompt,
        "image": f"data:image/png;base64,{image_b64}",
        "user_id": user_id,
    }
    if model:
        payload["model"] = model

    logger.info(
        f"[PROXY] Sending request to {proxy_url}, model={model}, size={len(image_bytes)}"
    )

    try:
        async with session.post(
            proxy_url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"Content-Type": "application/json"},
        ) as resp:
            if resp.status == 200:
                result_json = await resp.json()
                data_list = result_json.get("data", [])
                if data_list and isinstance(data_list, list):
                    b64_result = data_list[0].get("b64_json")
                    if b64_result:
                        result = base64.b64decode(b64_result)
                        logger.info(f"[PROXY] Success: received {len(result)} bytes")
                        return result
                logger.error(f"[PROXY] Invalid response structure: missing b64_json")
                return None
            else:
                error_text = await resp.text()
                logger.error(f"[PROXY] HTTP Error {resp.status}: {error_text[:200]}")
                return None
    except asyncio.TimeoutError:
        logger.error(f"[PROXY] Timeout after {timeout}s")
        return None
    except Exception as exc:
        logger.error(f"[PROXY] Request failed: {exc}")
        return None


async def _upload_to_owui_files(
    session: aiohttp.ClientSession,
    owui_base: str,
    image_bytes: bytes,
    filename: str,
    auth_token: Optional[str] = None,
) -> Optional[str]:
    url = f"{owui_base.rstrip('/')}/api/v1/files/"
    headers = {"accept": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    form = aiohttp.FormData()
    form.add_field(
        "file", io.BytesIO(image_bytes), filename=filename, content_type="image/png"
    )

    logger.info(
        f"[UPLOAD] Attempting upload to {url}, filename={filename}, token_present={bool(auth_token)}"
    )

    try:
        async with session.post(url, data=form, headers=headers) as resp:
            response_text = await resp.text()
            logger.debug(
                f"[UPLOAD] Response status={resp.status}, body={response_text[:200]}"
            )

            if resp.status in (200, 201):
                try:
                    data = json.loads(response_text)
                    fid = data.get("id")
                    if fid:
                        content_url = f"/api/v1/files/{fid}/content"
                        logger.info(f"[UPLOAD] Success: {content_url}")
                        return content_url
                    logger.warning(f"[UPLOAD] Missing 'id' in response: {data}")
                except json.JSONDecodeError:
                    logger.warning(f"[UPLOAD] Invalid JSON response")
            else:
                logger.error(f"[UPLOAD] Failed with HTTP {resp.status}")
    except Exception as exc:
        logger.error(f"[UPLOAD] Exception during upload: {exc}")
    return None


def _to_data_uri(image_bytes: bytes, mime_type: str = "image/png") -> str:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{b64}"


class Tools:
    class Valves(BaseModel):
        proxy_url: str = Field(
            default=PROXY_URL_DEFAULT,
            description="URL of the image editing proxy endpoint.",
        )
        model: str = Field(
            default="imageed-private",
            description="Model name to send to the balancer.",
        )
        owui_internal_base: str = Field(
            default=OWUI_INTERNAL_BASE, description="Internal base URL for OpenWebUI."
        )
        proxy_timeout: int = Field(
            default=300, description="Timeout in seconds for proxy requests."
        )
        fallback_to_base64: bool = Field(
            default=True, description="Fallback to base64 if upload fails."
        )
        show_model_name: bool = Field(
            default=True, description="Показывать имя модели в статусах."
        )

    def __init__(self):
        self.valves = self.Valves()
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _cleanup_session(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def resolve_model_name(self, model_id: str) -> str:
        try:
            from open_webui.models.models import Models

            model_obj = await Models.get_model_by_id(model_id)
            if not model_obj:
                return model_id
            name = getattr(model_obj, "name", None)
            if name and isinstance(name, str) and name.strip():
                return name
            if hasattr(model_obj, "__dict__"):
                data = model_obj.__dict__
                if "name" in data and data["name"]:
                    return data["name"]
            base_id = getattr(model_obj, "base_model_id", None)
            if base_id and isinstance(base_id, str) and base_id.strip():
                return base_id
            return model_id
        except Exception:
            return model_id

    async def emit_status(
        self,
        text: str,
        emitter: Any,
        emoji: str = "✨",
        done: bool = False,
        error: bool = False,
        extra_data: Optional[dict] = None,
        metadata: Optional[dict] = None,
    ):
        if not emitter or not callable(emitter):
            return

        prefix = ""
        if self.valves.show_model_name and metadata:
            model_id = None
            chat_info = metadata.get("chat", {})
            if isinstance(chat_info, dict):
                inner_chat = chat_info.get("chat", {})
                if isinstance(inner_chat, dict):
                    model_id = inner_chat.get("modelId")

            if not model_id:
                model_info = metadata.get("model", {})
                if isinstance(model_info, dict):
                    model_id = model_info.get("id")

            if model_id:
                display_name = await self.resolve_model_name(model_id)
                prefix = f"{display_name}: "

        description = f"{emoji} {prefix}{text}"

        try:
            await emitter(
                {
                    "type": "status",
                    "data": {
                        "description": description,
                        "done": done,
                        "error": error,
                        **(extra_data or {}),
                    },
                }
            )
        except Exception as e:
            logger.error(f"[EMIT] Status failed: {e}")

    async def emit_files(self, files: list[dict], emitter: Any):
        if not emitter or not callable(emitter):
            logger.warning("[EMIT] Files skipped: emitter not callable")
            return
        try:
            logger.info(f"[EMIT] Sending files: {files}")
            await emitter(
                {
                    "type": "files",
                    "data": {"files": files},
                }
            )
        except Exception as e:
            logger.error(f"[EMIT] Files failed: {e}")

    def _get_auth_token(self, user: Optional[Dict[str, Any]]) -> Optional[str]:
        if user and isinstance(user, dict):
            return user.get("token")
        return None

    async def edit_proxy(
        self,
        prompt: str,
        image_url: str = "",
        __event_emitter__: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
        __user__: Optional[Dict[str, Any]] = None,
        __messages__: Optional[List[Dict[str, Any]]] = None,
        __metadata__: Optional[Dict[str, Any]] = None,
        __request__: Optional[Any] = None,
    ) -> str:
        emitter = __event_emitter__
        owui_base = self.valves.owui_internal_base
        proxy_url = self.valves.proxy_url.rstrip("/")
        auth_token = self._get_auth_token(__user__)
        user_id = __user__.get("id") if __user__ else None

        logger.info(
            f"[TOOL] edit_proxy called. user_id={user_id}, has_token={bool(auth_token)}, emitter_callable={callable(emitter)}"
        )

        session = await self._get_session()

        try:
            await self.emit_status(
                text="Поиск изображения…",
                emoji="🔍",
                emitter=emitter,
                done=False,
                metadata=__metadata__,
            )

            candidate_urls: List[str] = []
            if image_url and image_url.strip():
                candidate_urls.append(image_url.strip())
            if __messages__:
                msg_urls = _extract_image_urls_from_messages(__messages__)
                candidate_urls.extend([u for u in msg_urls if u not in candidate_urls])

            logger.info(f"[TOOL] Candidate URLs: {candidate_urls}")

            image_bytes: Optional[bytes] = None
            for url in candidate_urls:
                image_bytes = await _bytes_from_url(session, url, owui_base, auth_token)
                if image_bytes:
                    break

            if not image_bytes:
                await self.emit_status(
                    text="Не удалось найти изображение.",
                    emoji="❌",
                    emitter=emitter,
                    done=True,
                    error=True,
                    metadata=__metadata__,
                )
                return "❌ Не удалось извлечь изображение."

            await self.emit_status(
                text="Отправка на редактирование…",
                emoji="🎨",
                emitter=emitter,
                done=False,
                metadata=__metadata__,
            )

            result_bytes = await _send_to_proxy(
                session=session,
                proxy_url=proxy_url,
                prompt=prompt,
                image_bytes=image_bytes,
                user_id=user_id,
                model=self.valves.model,
                timeout=self.valves.proxy_timeout,
            )

            if not result_bytes:
                await self.emit_status(
                    text="Ошибка прокси.",
                    emoji="❌",
                    emitter=emitter,
                    done=True,
                    error=True,
                    metadata=__metadata__,
                )
                return "❌ Прокси не вернул результат."

            await self.emit_status(
                text="Сохранение результата…",
                emoji="💾",
                emitter=emitter,
                done=False,
                metadata=__metadata__,
            )

            result_filename = f"edit_{uuid.uuid4().hex[:8]}.png"
            display_url = await _upload_to_owui_files(
                session, owui_base, result_bytes, result_filename, auth_token
            )

            use_fallback = False
            if not display_url:
                if self.valves.fallback_to_base64:
                    logger.warning("[TOOL] Upload failed, using base64 fallback")
                    display_url = _to_data_uri(result_bytes)
                    use_fallback = True
                else:
                    await self.emit_status(
                        text="Не удалось сохранить результат.",
                        emoji="❌",
                        emitter=emitter,
                        done=True,
                        error=True,
                        metadata=__metadata__,
                    )
                    return "❌ Результат получен, но не удалось сохранить."

            file_entry = {
                "type": "image",
                "url": display_url,
                "name": result_filename,
                "collection_name": "",
            }

            logger.info(f"[TOOL] Emitting file: {file_entry}")
            await self.emit_files(files=[file_entry], emitter=emitter)

            await self.emit_status(
                text="Редактирование завершено!",
                emoji="✅",
                emitter=emitter,
                done=True,
                metadata=__metadata__,
            )

            result_msg = f'Изображение успешно отредактировано. Запрос: "{prompt}"'
            #            if use_fallback:
            #                result_msg += "\n\n⚠️ *Примечание: Файл возвращен в формате base64, так как загрузка на сервер не удалась.*"

            return result_msg

        finally:
            await self._cleanup_session()
