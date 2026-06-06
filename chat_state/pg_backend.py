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
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
#

import logging
from typing import Optional
import asyncpg
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
                # ✅ CORRECT: Simple composite primary key (no functions)
                await conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {self.table} (
                        openweb_id TEXT NOT NULL,
                        model TEXT,
                        qwen_chat_id TEXT NOT NULL,
                        last_parent_id TEXT,
                        is_new BOOLEAN DEFAULT FALSE,
                        created_at DOUBLE PRECISION,
                        updated_at TIMESTAMP DEFAULT NOW(),
                        PRIMARY KEY (openweb_id, model)
                    )
                """)

                # 🔧 Wrap index creation in try/except to handle race conditions
                try:
                    await conn.execute(f"""
                        CREATE INDEX IF NOT EXISTS idx_{self.table}_updated 
                        ON {self.table}(updated_at)
                    """)
                except (asyncpg.exceptions.UniqueViolationError, 
                        asyncpg.exceptions.DuplicateTableError) as e:
                    logger.debug(f"⚡ Index idx_{self.table}_updated already exists (race, safe): {e}")
                except Exception as e:
                    # Check if it's a duplicate error in the message
                    err_msg = str(e).lower()
                    if "повторяющееся" in err_msg or "duplicate" in err_msg or "already exists" in err_msg:
                        logger.debug(f"⚡ Index idx_{self.table}_updated already exists (race, safe)")
                    else:
                        raise

            return True
        except Exception as e:
            logger.error(f"PostgresBackend init error: {e}")
            return False

    async def close(self):
        pass

    def _normalize_model(self, model: Optional[str]) -> str:
        """
        Normalizes the model for storage in the database:
        - None or empty string → '' (empty string)
        - Any other value → as is
        This ensures backward compatibility with legacy model-less records.
        """
        return model or ""

    async def get(self, openweb_id: str, model: Optional[str] = None) -> Optional[ChatStateData]:
        pool = get_state_db_pool()
        if not pool:
            return None
        try:
            async with pool.acquire() as conn:
                # ✅ Normalize the model before querying
                norm_model = self._normalize_model(model)

                # Trying to find a record with a normalized model
                row = await conn.fetchrow(
                    f"""
                    SELECT * FROM {self.table} 
                    WHERE openweb_id = $1 AND model = $2
                    """,
                    openweb_id,
                    norm_model
                )

                if row:
                    # Return the model as is (maybe None for legacy)
                    return ChatStateData(
                        qwen_chat_id=row["qwen_chat_id"],
                        last_parent_id=row["last_parent_id"],
                        is_new=row["is_new"],
                        created_at=row["created_at"] or 0.0,
                        model=row["model"] if row["model"] else model
                    )

                # Fallback: If the model was specified but not found, we look for the legacy record (model IS NULL)
                if model:
                    fallback = await conn.fetchrow(
                        f"""
                        SELECT * FROM {self.table} 
                        WHERE openweb_id = $1 AND model IS NULL
                        """,
                        openweb_id
                    )
                    if fallback:
                        logger.debug(f"🔄 Fallback to legacy state for {openweb_id}")
                        return ChatStateData(
                            qwen_chat_id=fallback["qwen_chat_id"],
                            last_parent_id=fallback["last_parent_id"],
                            is_new=fallback["is_new"],
                            created_at=fallback["created_at"] or 0.0,
                            model=None
                        )

                return None
        except Exception as e:
            logger.error(f"PostgresBackend.get error: {e}")
            return None

    async def set(self, openweb_id: str, data: ChatStateData, model: Optional[str] = None):
        pool = get_state_db_pool()
        if not pool:
            return
        try:
            effective_model = self._normalize_model(model or data.model)
            async with pool.acquire() as conn:
                await conn.execute(f"""
                    INSERT INTO {self.table} 
                    (openweb_id, model, qwen_chat_id, last_parent_id, is_new, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, NOW())
                    ON CONFLICT (openweb_id, model) DO UPDATE SET
                        qwen_chat_id = EXCLUDED.qwen_chat_id,
                        last_parent_id = EXCLUDED.last_parent_id,
                        is_new = EXCLUDED.is_new,
                        created_at = EXCLUDED.created_at,
                        updated_at = NOW()
                """, openweb_id, effective_model, data.qwen_chat_id, 
                   data.last_parent_id, data.is_new, data.created_at)
        except Exception as e:
            logger.error(f"PostgresBackend.set error: {e}")
            raise

    async def update_parent(self, openweb_id: str, parent_id: str, model: Optional[str] = None):
        pool = get_state_db_pool()
        if not pool:
            return
        try:
            norm_model = self._normalize_model(model)
            async with pool.acquire() as conn:
                await conn.execute(f"""
                    UPDATE {self.table} 
                    SET last_parent_id = $1, is_new = FALSE, updated_at = NOW()
                    WHERE openweb_id = $2 AND model = $3
                """, parent_id, openweb_id, norm_model)
        except Exception as e:
            logger.error(f"PostgresBackend.update_parent error: {e}")
            raise

    async def delete(self, openweb_id: str, model: Optional[str] = None):
        pool = get_state_db_pool()
        if not pool:
            return
        try:
            norm_model = self._normalize_model(model)
            async with pool.acquire() as conn:
                result = await conn.execute(
                    f"DELETE FROM {self.table} WHERE openweb_id = $1 AND model = $2",
                    openweb_id, norm_model
                )
                return int(result.split()[-1]) > 0
        except Exception as e:
            logger.error(f"PostgresBackend.delete error: {e}")
            raise
