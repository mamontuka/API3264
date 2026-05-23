# Copyright (C) 2026
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

import logging
from typing import Optional
from config import Config, TokenBackendType
from .base import TokenBackend
from .file_backend import FileTokenBackend
from .pg_backend import PgTokenBackend

logger = logging.getLogger("FreeQwenApi")

_backend: Optional[TokenBackend] = None
_fallback_active: bool = False

async def _create_backend(mode: TokenBackendType) -> Optional[TokenBackend]:
    try:
        if mode == TokenBackendType.POSTGRES:
            backend = PgTokenBackend(
                host=Config.TOKEN_DB_HOST,
                port=Config.TOKEN_DB_PORT,
                user=Config.TOKEN_DB_USER,
                password=Config.TOKEN_DB_PASSWORD,
                dbname=Config.TOKEN_DB_NAME,
                table=Config.TOKEN_DB_TABLE,
                ssl_mode=Config.TOKEN_DB_SSL_MODE
            )
            if not await backend.init(): return None
            if not await backend.health_check(): return None
            return backend
        else:
            backend = FileTokenBackend(Config.TOKENS_FILE)
            return backend if await backend.init() else None
    except Exception as e:
        logger.error(f"Token backend create error: {e}")
        return None

async def init_token_storage() -> TokenBackend:
    global _backend, _fallback_active
    if _backend: return _backend
    
    target = Config.TOKEN_STORAGE_BACKEND
    backend = await _create_backend(target)
    
    if backend:
        _backend = backend
        _fallback_active = False
        logger.info(f"✅ TokenStorage backend initialized: {target.value}")
        return _backend
    
    if target == TokenBackendType.POSTGRES:
        logger.warning("⚠️ Token PostgreSQL unavailable, fallback to file...")
        fb = await _create_backend(TokenBackendType.FILE)
        if fb:
            _backend = fb
            _fallback_active = True
            logger.warning("✅ TokenStorage works in fallback file mode.")
            return _backend
            
    raise RuntimeError("Failed to initialize any token backend.")

async def close_token_storage():
    global _backend, _fallback_active
    if _backend:
        await _backend.close()
        _backend = None
        _fallback_active = False

def get_token_backend() -> TokenBackend:
    if not _backend: raise RuntimeError("Token backend not initialized.")
    return _backend

def is_token_fallback_active() -> bool:
    return _fallback_active
