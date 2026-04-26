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
#
#

import logging
from typing import Optional
from config import Config, ChatStateBackendType
from .base import ChatStateBackend
from .file_backend import FileBackend
from .pg_backend import PostgresBackend
from .db_client import init_state_db_pool, close_state_db_pool

logger = logging.getLogger("FreeQwenApi")

_backend: Optional[ChatStateBackend] = None
_fallback_active: bool = False

async def _create_and_init_backend(backend_type: ChatStateBackendType) -> Optional[ChatStateBackend]:
    """Create and init backend, return None on error."""
    try:
        backend: ChatStateBackend

        if backend_type == ChatStateBackendType.POSTGRES:
            logger.info("🗄 Initialization ChatState: PostgreSQL")
            # Init pool first
            pool = await init_state_db_pool()
            if pool is None:
                logger.error("❌ Failed creating connection pool for PostgreSQL.")
                return None

            backend = PostgresBackend(table=Config.CHAT_STATE_DB_TABLE)

            # Critical: check DB health before use
            if not await backend.health_check():
                logger.error("❌ Failed PostgreSQL health check.")
                return None

        else:
            logger.info("💾 Initialization ChatState: File")
            backend = FileBackend(Config.CHAT_STATE_FILE)

        # Overall initialization
        success = await backend.init()
        if not success:
            logger.error(f"❌ Error backend initialization {backend_type}.")
            return None

        return backend

    except Exception as e:
        logger.error(f"❌ Exception on backend create {backend_type}: {type(e).__name__}: {e}")
        return None

async def init_chat_state() -> ChatStateBackend:
    global _backend, _fallback_active

    if _backend is not None:
        return _backend

    target_mode = Config.CHAT_STATE_BACKEND
    backend = await _create_and_init_backend(target_mode)

    if backend is not None:
        _backend = backend
        _fallback_active = False
        logger.info(f"✅ ChatState successful started in mode: {target_mode.value}")
        return _backend

    # Fallback logic
    if target_mode == ChatStateBackendType.POSTGRES:
        logger.warning("⚠️ PostgreSQL not available. Crash switch to file mode...")
        fallback_backend = await _create_and_init_backend(ChatStateBackendType.FILE)

        if fallback_backend is not None:
            _backend = fallback_backend
            _fallback_active = True
            logger.warning("✅ ChatState wirks in crash file mode.")
            return _backend
        else:
            logger.critical("💀 Critical error: BOTH MODES - PostgreSQL AND file NOT WORK!.")
            raise RuntimeError("Failed to initialize any chat state backend. System cannot start.")
    else:
        # If file mode dont start its critical
        logger.critical("💀 File mode critical error.")
        raise RuntimeError("Failed to initialize file chat state backend.")

async def close_chat_state():
    global _backend, _fallback_active
    if _backend:
        await _backend.close()
        _backend = None
        _fallback_active = False

    # Close pool
    await close_state_db_pool()

def get_chat_state_backend() -> ChatStateBackend:
    if _backend is None:
        raise RuntimeError("Chat state backend not initialized. Call init_chat_state() first.")
    return _backend

def is_fallback_active() -> bool:
    """Return True, if system works in fallback mode."""
    return _fallback_active
