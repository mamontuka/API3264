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

"""
Asynchronous Database Module for FreeQwenApi Proxy
==================================================
Replaces synchronous psycopg2 calls with native asyncpg driver.
This prevents Event Loop blocking during database operations.

Key Features:
    • Connection pooling (efficient resource usage)
    • Native async/await support (no blocking)
    • Automatic reconnection on failure
    • Row factory for dict-like access

Usage:
    from db_async import get_db_pool, fetch_chat_id

    # In lifespan handler:
    pool = await init_db_pool()

    # In request handler:
    chat_id = await fetch_chat_id_from_db(user_id)
"""
import asyncio
import logging
from typing import Optional, Dict, Any
import asyncpg

# Import configuration
from config import Config

logger = logging.getLogger("FreeQwenApi")

# Global connection pool
_db_pool: Optional[asyncpg.Pool] = None
_pool_lock = asyncio.Lock()


async def init_db_pool() -> Optional[asyncpg.Pool]:
    """
    Initialize PostgreSQL connection pool using asyncpg.

    🔥 This replaces get_pg_connection() from config.py.
    Pool is created once and reused across all requests.

    Returns:
        asyncpg.Pool: Connection pool or None if DB disabled
    """
    global _db_pool

    if not Config.OPENWEBUI_DB_ENABLED:
        logger.debug("🗄 Database disabled in config")
        return None

    async with _pool_lock:
        if _db_pool is not None:
            # Pool already initialized
            return _db_pool

        try:
            logger.info(f"🗄 Creating asyncpg pool: {Config.OPENWEBUI_DB_HOST}:{Config.OPENWEBUI_DB_PORT}/{Config.OPENWEBUI_DB_NAME}")

            _db_pool = await asyncpg.create_pool(
                host=Config.OPENWEBUI_DB_HOST,
                port=Config.OPENWEBUI_DB_PORT,
                database=Config.OPENWEBUI_DB_NAME,
                user=Config.OPENWEBUI_DB_USER,
                password=Config.OPENWEBUI_DB_PASSWORD,
                ssl=Config.OPENWEBUI_DB_SSL_MODE != "disable",
                # 🔥 Pool settings tuned for proxy workload
                min_size=2,
                max_size=10,
                command_timeout=Config.OPENWEBUI_DB_CONNECT_TIMEOUT * 2,
                connection_class=asyncpg.Connection,
            )

            logger.info(f"🗄 Asyncpg pool created successfully (min={_db_pool.get_min_size()}, max={_db_pool.get_max_size()})")
            return _db_pool

        except Exception as e:
            logger.error(f"❌ Failed to create asyncpg pool: {type(e).__name__}: {e}")
            return None


async def close_db_pool():
    """
    Close all connections in the pool.
    Call this during application shutdown.
    """
    global _db_pool

    if _db_pool is not None:
        logger.info("🗄 Closing asyncpg pool...")
        await _db_pool.close()
        _db_pool = None
        logger.debug("🗄 All DB connections closed")


async def fetch_chat_id_from_db(user_id: str, conversation_title: Optional[str] = None) -> Optional[str]:
    """
    Get stable chat ID from OpenWebUI PostgreSQL database (async version).

    🔥 REPLACES: _get_openwebui_chat_id_from_db() logic from qwenapi.py

    Args:
        user_id: OpenWebUI user identifier
        conversation_title: Optional title filter (not used currently)

    Returns:
        str|None: Chat ID from database, or None if not found
    """
    if _db_pool is None:
        return None

    try:
        async with _db_pool.acquire() as conn:
            # 🔥 Async query - does NOT block Event Loop
            row = await conn.fetchrow("""
                SELECT id, title, user_id, updated_at
                FROM chat
                WHERE user_id = $1
                ORDER BY updated_at DESC
                LIMIT 1
            """, user_id)

            if row:
                logger.debug(f"🗄 Found chat in DB: id={str(row['id'])[:8]}..., user_id={str(row['user_id'])[:8]}..., updated_at={row['updated_at']}")
                return str(row['id'])

            logger.debug(f"🗄 No chat found for user_id={user_id}")
            return None

    except asyncpg.PostgresError as e:
        logger.warning(f"⚠️ Postgres error querying chat: {type(e).__name__}: {e}")
        return None
    except Exception as e:
        logger.warning(f"⚠️ Error querying OpenWebUI DB: {type(e).__name__}: {e}")
        return None


async def test_db_connection() -> bool:
    """
    Test database connectivity (for health checks).

    Returns:
        bool: True if connection successful, False otherwise
    """
    if _db_pool is None:
        return False

    try:
        async with _db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
            return True
    except Exception as e:
        logger.debug(f"⚠️ DB health check failed: {e}")
        return False
