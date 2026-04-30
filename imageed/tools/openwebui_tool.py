"""
title: Image Edit Proxy
description: >
  Редактирование изображений через внешний прокси.
  Извлекает изображения из чата, отправляет на прокси и возвращает результат.
  Поддерживает base64, /api/v1/files/{id}/content и прямые URL.
author: Oleh Mamont - https://github.com/mamontuka
version: 1.3
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

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация по умолчанию из переменных окружения
OWUI_INTERNAL_BASE = os.environ.get("OWUI_INTERNAL_BASE", "http://127.0.0.1:8181")
PROXY_URL_DEFAULT = os.environ.get(
    "EDIT_PROXY_URL", "http://10.32.64.2:7264/v1/images/edits"
)


# ─── Image Extraction Helpers ──────────────────────────────────────────────────


def _extract_image_urls_from_messages(messages: List[Dict[str, Any]]) -> List[str]:
    """
    Walk messages newest-first and collect all image_url content blocks.
    Returns URLs in discovery order.
    """
    found: List[str] = []
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
    """
    Resolve image bytes from any URL format OpenWebUI can produce using shared session.
      1. data:image/...;base64,...  → decode inline
      2. /api/v1/files/{id}/content → GET from OWUI server-side base with auth
      3. http(s)://...              → GET directly
    """
    if not url:
        return None

    # Case 1: Inline base64
    if url.startswith("data:"):
        try:
            _, data = url.split(",", 1)
            return base64.b64decode(data)
        except Exception as exc:
            logger.warning("base64 decode failed: %s", exc)
            return None

    # Case 2: Internal OWUI file URL
    if url.startswith("/"):
        full_url = owui_base.rstrip("/") + url
        headers = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        try:
            async with session.get(full_url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    logger.info("Fetched OWUI internal image (%d bytes)", len(data))
                    return data
                logger.warning("OWUI fetch %s → HTTP %d", url, resp.status)
        except Exception as exc:
            logger.warning("OWUI fetch error: %s", exc)
        return None

    # Case 3: External URL
    if url.startswith("http://") or url.startswith("https://"):
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    logger.info("Fetched image from URL (%d bytes)", len(data))
                    return data
                logger.warning("URL fetch %s → HTTP %d", url, resp.status)
        except Exception as exc:
            logger.warning("URL fetch error: %s", exc)
        return None

    logger.warning("Unrecognised image URL format: %.80s", url)
    return None


# ─── Proxy Communication ───────────────────────────────────────────────────────


async def _send_to_proxy(
    session: aiohttp.ClientSession,
    proxy_url: str,
    prompt: str,
    image_bytes: bytes,
    timeout: int = 300,
) -> Optional[bytes]:
    """
    Send image and prompt to the editing proxy using shared session.
    Sends JSON payload with base64-encoded image.
    Expects proxy to return JSON with data[0].b64_json field.
    """
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {"prompt": prompt, "image": f"data:image/png;base64,{image_b64}"}

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
                        logger.info(
                            "Proxy returned edited image via JSON (%d bytes)",
                            len(result),
                        )
                        return result
                logger.error("Proxy response missing data[0].b64_json: %s", result_json)
                return None
            else:
                error_text = await resp.text()
                logger.error("Proxy error HTTP %d: %s", resp.status, error_text[:200])
                return None
    except asyncio.TimeoutError:
        logger.error("Proxy request timed out after %ds", timeout)
        return None
    except Exception as exc:
        logger.error("Proxy request failed: %s", exc)
        return None


# ─── Result Storage ────────────────────────────────────────────────────────────


async def _upload_to_owui_files(
    session: aiohttp.ClientSession,
    owui_base: str,
    image_bytes: bytes,
    filename: str,
    auth_token: Optional[str] = None,
) -> Optional[str]:
    """
    Upload result image to OpenWebUI files API using shared session.
    Returns /api/v1/files/{id}/content on success.
    """
    url = f"{owui_base.rstrip('/')}/api/v1/files/"
    headers = {"accept": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    form = aiohttp.FormData()
    form.add_field(
        "file", io.BytesIO(image_bytes), filename=filename, content_type="image/png"
    )

    try:
        async with session.post(url, data=form, headers=headers) as resp:
            if resp.status in (200, 201):
                data = await resp.json()
                fid = data.get("id")
                if fid:
                    content_url = f"/api/v1/files/{fid}/content"
                    logger.info("Stored result in OWUI: %s", content_url)
                    return content_url
                logger.warning("OWUI response missing 'id': %s", data)
            else:
                logger.warning(
                    "OWUI upload failed HTTP %d: %s",
                    resp.status,
                    (await resp.text())[:200],
                )
    except Exception as exc:
        logger.warning("OWUI upload error: %s", exc)
    return None


def _to_data_uri(image_bytes: bytes, mime_type: str = "image/png") -> str:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{b64}"


# ─── Main Tool Class ───────────────────────────────────────────────────────────


class Tools:
    class Valves(BaseModel):
        proxy_url: str = Field(
            default=PROXY_URL_DEFAULT,
            description="URL of the image editing proxy endpoint.",
        )
        owui_internal_base: str = Field(
            default=OWUI_INTERNAL_BASE,
            description="Internal base URL for OpenWebUI (e.g., http://localhost:8080).",
        )
        proxy_timeout: int = Field(
            default=300, description="Timeout in seconds for proxy requests."
        )
        fallback_to_base64: bool = Field(
            default=True,
            description="If OWUI file upload fails, return result as base64 data URI.",
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

    async def _emit_status(
        self,
        emitter: Optional[Callable],
        text: str,
        done: bool = False,
        error: bool = False,
    ) -> None:
        if emitter:
            await emitter(
                {
                    "type": "status",
                    "data": {"description": text, "done": done, "error": error},
                }
            )

    async def _emit_image(
        self, emitter: Optional[Callable], display_url: str, label: str
    ) -> None:
        if not emitter or not display_url:
            return
        await emitter(
            {
                "type": "files",
                "data": {
                    "files": [
                        {
                            "type": "image",
                            "url": display_url,
                            "name": label,
                            "collection_name": "",
                        }
                    ]
                },
            }
        )

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
        __request__: Optional[Any] = None,
    ) -> str:
        """
        Редактировать изображение через прокси.
        Исправленная версия 1.3: корректная сигнатура, оптимизация сессий, надежный токен.
        """
        emitter = __event_emitter__
        owui_base = self.valves.owui_internal_base
        proxy_url = self.valves.proxy_url.rstrip("/")

        auth_token = self._get_auth_token(__user__)

        session = await self._get_session()

        try:
            # ── 1. Resolve image ───────────────────────────────────────────────────
            await self._emit_status(emitter, "🔍 Поиск изображения…")
            candidate_urls: List[str] = []

            if image_url and image_url.strip():
                candidate_urls.append(image_url.strip())

            if __messages__:
                msg_urls = _extract_image_urls_from_messages(__messages__)
                candidate_urls.extend([u for u in msg_urls if u not in candidate_urls])

            image_bytes: Optional[bytes] = None
            for url in candidate_urls:
                image_bytes = await _bytes_from_url(session, url, owui_base, auth_token)
                if image_bytes:
                    break

            if not image_bytes:
                await self._emit_status(
                    emitter, "❌ Не удалось найти изображение.", done=True, error=True
                )
                return (
                    "❌ Не удалось извлечь изображение. Убедитесь, что вы прикрепили файл "
                    "или передали корректный image_url."
                )

            logger.info("Resolved image for editing (%d bytes)", len(image_bytes))

            # ── 2. Send to proxy ───────────────────────────────────────────────────
            await self._emit_status(emitter, "🎨 Отправка на редактирование…")
            result_bytes = await _send_to_proxy(
                session=session,
                proxy_url=proxy_url,
                prompt=prompt,
                image_bytes=image_bytes,
                timeout=self.valves.proxy_timeout,
            )

            if not result_bytes:
                await self._emit_status(
                    emitter, "❌ Ошибка прокси.", done=True, error=True
                )
                return "❌ Прокси не вернул результат. Проверьте логи сервера."

            # ── 3. Store and return result ─────────────────────────────────────────
            await self._emit_status(emitter, "💾 Сохранение результата…")
            result_filename = f"edit_{uuid.uuid4().hex[:8]}.png"
            display_url = await _upload_to_owui_files(
                session, owui_base, result_bytes, result_filename, auth_token
            )

            if not display_url and self.valves.fallback_to_base64:
                logger.info("OWUI upload failed, falling back to base64")
                display_url = _to_data_uri(result_bytes)

            if not display_url:
                await self._emit_status(
                    emitter, "❌ Не удалось сохранить результат.", done=True, error=True
                )
                return (
                    "❌ Результат получен, но не удалось сохранить или отобразить его."
                )

            await self._emit_image(emitter, display_url, result_filename)
            await self._emit_status(emitter, "✅ Редактирование завершено!", done=True)

            return (
                f"Изображение успешно отредактировано. Результат прикреплён к сообщению. "
                f'Запрос: "{prompt}"'
            )
        finally:
            await self._cleanup_session()
