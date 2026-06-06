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
from .base import ChatStateBackend, ChatStateData
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
            pool = await init_state_db_pool()
            if pool is None:
                logger.error("❌ Failed creating connection pool for PostgreSQL.")
                return None

            backend = PostgresBackend(table=Config.CHAT_STATE_DB_TABLE)

            if not await backend.health_check():
                logger.error("❌ Failed PostgreSQL health check.")
                return None

        else:
            logger.info("💾 Initialization ChatState: File")
            backend = FileBackend(Config.CHAT_STATE_FILE)

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

    if target_mode == ChatStateBackendType.POSTGRES:
        logger.warning("⚠️ PostgreSQL not available. Crash switch to file mode...")
        fallback_backend = await _create_and_init_backend(ChatStateBackendType.FILE)

        if fallback_backend is not None:
            _backend = fallback_backend
            _fallback_active = True
            logger.warning("✅ ChatState works in crash file mode.")
            return _backend
        else:
            logger.critical("💀 Critical error: BOTH MODES - PostgreSQL AND file NOT WORK!.")
            raise RuntimeError("Failed to initialize any chat state backend. System cannot start.")
    else:
        logger.critical("💀 File mode critical error.")
        raise RuntimeError("Failed to initialize file chat state backend.")

async def close_chat_state():
    global _backend, _fallback_active
    if _backend:
        await _backend.close()
        _backend = None
        _fallback_active = False

    await close_state_db_pool()

def get_chat_state_backend() -> ChatStateBackend:
    if _backend is None:
        raise RuntimeError("Chat state backend not initialized. Call init_chat_state() first.")
    return _backend

def is_fallback_active() -> bool:
    """Return True, if system works in fallback mode."""
    return _fallback_active


# =============================================================================
# 🔧 GLOBAL HELPERS WITH ISOLATION SUPPORT BY MODELS
# =============================================================================

async def get_chat_state(openweb_id: str, model: Optional[str] = None) -> Optional[ChatStateData]:
    """
    Get chat state with model-based isolation support.

    Args:
        openweb_id: OpenWebUI chat ID
        model: State isolation model (optional).
               If specified, the composite key openweb_id:model will be used.
               If not specified, the base key is used (backward compatibility).

    Returns:
        ChatStateData or None if the state is not found.
    """
    backend = get_chat_state_backend()
    return await backend.get(openweb_id, model=model)


async def set_chat_state(openweb_id: str, data: ChatStateData, model: Optional[str] = None):
    """
    Save chat state with model isolation support.

    Args:
        openweb_id: OpenWebUI chat ID
        data: State data
        model: Insulation model (optional)
    """
    backend = get_chat_state_backend()
    await backend.set(openweb_id, data, model=model)


async def update_chat_parent_id(openweb_id: str, parent_id: str, model: Optional[str] = None):
    """
    Update last_parent_id with model isolation support.

    Args:
        openweb_id: OpenWebUI chat ID
        parent_id: New parent_id
        model: Insulation model (optional)
    """
    backend = get_chat_state_backend()
    await backend.update_parent(openweb_id, parent_id, model=model)


async def delete_chat_state(openweb_id: str, model: Optional[str] = None):
    """
    Remove chat state with model isolation support.

    Args:
        openweb_id: OpenWebUI chat ID
        model: Insulation model (optional)
    """
    backend = get_chat_state_backend()
    await backend.delete(openweb_id, model=model)
