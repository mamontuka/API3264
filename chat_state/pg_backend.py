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
from .base import ChatStateBackend, ChatStateData
from .db_client import get_state_db_pool

logger = logging.getLogger("FreeQwenApi")

class PostgresBackend(ChatStateBackend):
    def __init__(self, table: str):
        self.table = table

    async def health_check(self) -> bool:
        """Check database available."""
        pool = get_state_db_pool()
        if not pool:
            return False
        try:
            async with pool.acquire(timeout=5) as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception as e:
            logger.debug(f"Health check failed: {e}")
            return False

    async def init(self) -> bool:
        pool = get_state_db_pool()
        if not pool:
            logger.error("PostgresBackend init failed: pool is None")
            return False

        try:
            async with pool.acquire() as conn:
                # Create table in our own DB
                await conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {self.table} (
                        openweb_id TEXT PRIMARY KEY,
                        qwen_chat_id TEXT NOT NULL,
                        last_parent_id TEXT,
                        is_new BOOLEAN DEFAULT FALSE,
                        created_at DOUBLE PRECISION,
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """)
                await conn.execute(f"""
                    CREATE INDEX IF NOT EXISTS idx_{self.table}_updated 
                    ON {self.table}(updated_at)
                """)
            return True
        except Exception as e:
            logger.error(f"PostgresBackend init error: {e}")
            return False

    async def close(self):
        pass  # Pool closing over close_state_db_pool()

    async def get(self, openweb_id: str) -> Optional[ChatStateData]:
        pool = get_state_db_pool()
        if not pool:
            return None
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT * FROM {self.table} WHERE openweb_id = $1",
                    openweb_id
                )
                if not row:
                    return None
                return ChatStateData(
                    qwen_chat_id=row["qwen_chat_id"],
                    last_parent_id=row["last_parent_id"],
                    is_new=row["is_new"],
                    created_at=row["created_at"] or 0.0
                )
        except Exception as e:
            logger.error(f"PostgresBackend.get error: {e}")
            return None

    async def set(self, openweb_id: str, data: ChatStateData):
        pool = get_state_db_pool()
        if not pool:
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute(f"""
                    INSERT INTO {self.table} 
                    (openweb_id, qwen_chat_id, last_parent_id, is_new, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, NOW())
                    ON CONFLICT (openweb_id) DO UPDATE SET
                        qwen_chat_id = EXCLUDED.qwen_chat_id,
                        last_parent_id = EXCLUDED.last_parent_id,
                        is_new = EXCLUDED.is_new,
                        created_at = EXCLUDED.created_at,
                        updated_at = NOW()
                """, openweb_id, data.qwen_chat_id, data.last_parent_id, data.is_new, data.created_at)
        except Exception as e:
            logger.error(f"PostgresBackend.set error: {e}")
            raise

    async def update_parent(self, openweb_id: str, parent_id: str):
        pool = get_state_db_pool()
        if not pool:
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute(f"""
                    UPDATE {self.table} 
                    SET last_parent_id = $1, is_new = FALSE, updated_at = NOW()
                    WHERE openweb_id = $2
                """, parent_id, openweb_id)
        except Exception as e:
            logger.error(f"PostgresBackend.update_parent error: {e}")
            raise

    async def delete(self, openweb_id: str):
        pool = get_state_db_pool()
        if not pool:
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute(f"DELETE FROM {self.table} WHERE openweb_id = $1", openweb_id)
        except Exception as e:
            logger.error(f"PostgresBackend.delete error: {e}")
            raise
