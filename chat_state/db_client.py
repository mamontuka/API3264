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

import asyncio
import logging
from typing import Optional
import asyncpg
from config import Config, ChatStateBackendType

logger = logging.getLogger("FreeQwenApi")

_state_db_pool: Optional[asyncpg.Pool] = None
_pool_lock = asyncio.Lock()

async def init_state_db_pool() -> Optional[asyncpg.Pool]:
    """Init independend pool for Chat State DB"""
    global _state_db_pool
    
    if Config.CHAT_STATE_BACKEND != ChatStateBackendType.POSTGRES:
        return None

    async with _pool_lock:
        if _state_db_pool is not None:
            return _state_db_pool

        try:
            logger.info(f"🗄 Creating Chat State DB pool: {Config.CHAT_STATE_DB_HOST}:{Config.CHAT_STATE_DB_PORT}/{Config.CHAT_STATE_DB_NAME}")
            _state_db_pool = await asyncpg.create_pool(
                host=Config.CHAT_STATE_DB_HOST,
                port=Config.CHAT_STATE_DB_PORT,
                database=Config.CHAT_STATE_DB_NAME,
                user=Config.CHAT_STATE_DB_USER,
                password=Config.CHAT_STATE_DB_PASSWORD,
                min_size=Config.CHAT_STATE_DB_POOL_MIN,
                max_size=Config.CHAT_STATE_DB_POOL_MAX,
                command_timeout=10,
            )
            logger.info(f"🗄 Chat State DB pool created (min={_state_db_pool.get_min_size()}, max={_state_db_pool.get_max_size()})")
            return _state_db_pool
        except Exception as e:
            logger.error(f"❌ Failed to create Chat State DB pool: {type(e).__name__}: {e}")
            # Dont raise exception here, for allow factory do fallback
            _state_db_pool = None
            return None

async def close_state_db_pool():
    """Close pool Chat State DB"""
    global _state_db_pool
    async with _pool_lock:
        if _state_db_pool is not None:
            logger.info("🗄 Closing Chat State DB pool...")
            try:
                await _state_db_pool.close()
            except Exception as e:
                logger.warning(f"Warning while closing pool: {e}")
            _state_db_pool = None

def get_state_db_pool() -> Optional[asyncpg.Pool]:
    return _state_db_pool
