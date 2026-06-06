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

"""
PostgreSQL token backend: FULL MIRROR MODE + COMPATIBILITY LAYER.
- Stores complete JSON objects in 'raw_data' column (as requested).
- Returns TokenData objects to maintain interface compatibility.
- Uses synchronous psycopg2 with asyncio.to_thread for uvicorn workers.
"""
import psycopg2
from psycopg2 import pool
import logging
import os
import json
import asyncio
from typing import List, Optional, Dict, Any

from config import Config
from .base import TokenBackend, TokenData

logger = logging.getLogger("FreeQwenApi")


class PgTokenBackend(TokenBackend):
    """PostgreSQL token backend with full mirror storage"""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        dbname: str,
        table: str,
        ssl_mode: str
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.dbname = dbname
        self.table = table
        self.ssl_mode = ssl_mode
        self.pool: Optional[pool.ThreadedConnectionPool] = None
        # Cache: {token_id: TokenData}
        self._cache: Dict[str, TokenData] = {}
        self._loaded: bool = False

    async def init(self) -> bool:
        """Initialize synchronous connection pool"""
        try:
            ssl_param = f"sslmode={self.ssl_mode}" if self.ssl_mode != "prefer" else ""
            conn_string = (
                f"host={self.host} port={self.port} dbname={self.dbname} "
                f"user={self.user} password={self.password} {ssl_param}"
            )

            self.pool = pool.ThreadedConnectionPool(
                minconn=Config.TOKEN_DB_POOL_MIN,
                maxconn=Config.TOKEN_DB_POOL_MAX,
                dsn=conn_string
            )

            # Ensure table exists (run in thread to not block)
            await asyncio.to_thread(self._ensure_table)

            logger.info(f"🗄 Creating Token Storage DB pool: {Config.TOKEN_DB_HOST}:{Config.TOKEN_DB_PORT}/{Config.TOKEN_DB_NAME}")
            logger.info(f"🗄 Token Storage DB pool created (min={Config.TOKEN_DB_POOL_MIN}, max={Config.TOKEN_DB_POOL_MAX})")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to create Token Storage DB pool: {e}")
            return False

    def _get_conn(self):
        return self.pool.getconn()

    def _release_conn(self, conn):
        self.pool.putconn(conn)

    def _ensure_table(self):
        """Create table with raw_data JSONB for full object storage"""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {self.table} (
                        id TEXT PRIMARY KEY,
                        raw_data JSONB NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW(),
                        last_used_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)

                # 🔧 Wrap index creation in try/except to handle race conditions
                try:
                    cur.execute(f"""
                        CREATE INDEX IF NOT EXISTS idx_{self.table}_updated 
                        ON {self.table}(updated_at)
                    """)
                except psycopg2.errors.UniqueViolation as e:
                    logger.debug(f"⚡ Index idx_{self.table}_updated already exists (race, safe): {e}")
                    conn.rollback()
                except psycopg2.errors.DuplicateTable as e:
                    logger.debug(f"⚡ Index idx_{self.table}_updated already exists (race, safe): {e}")
                    conn.rollback()
                except Exception as e:
                    # Check if it's a duplicate error in the message
                    err_msg = str(e).lower()
                    if "повторяющееся" in err_msg or "duplicate" in err_msg or "already exists" in err_msg:
                        logger.debug(f"⚡ Index idx_{self.table}_updated already exists (race, safe)")
                        conn.rollback()
                    else:
                        raise

            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Error ensuring table: {e}")
            raise
        finally:
            self._release_conn(conn)

    async def close(self):
        """Close pool and clear cache"""
        if self.pool:
            await asyncio.to_thread(self.pool.closeall)
            logger.info(f"Token pool closed (worker {os.getpid()})")
            self.pool = None
            self._cache.clear()
            self._loaded = False

    async def load_all(self) -> List[TokenData]:
        """Load ALL tokens from cache or DB, return as List[TokenData]"""
        if self._loaded:
            return list(self._cache.values())

        # Run DB query in thread to not block event loop
        rows = await asyncio.to_thread(self._fetch_all_rows)

        for token_id, raw_data in rows:
            # ✅ Convert raw_data (dict) -> TokenData
            token = TokenData.from_dict(raw_data)
            self._cache[token.id] = token

        self._loaded = True
        logger.info(f"📦 Loaded {len(self._cache)} tokens into cache (worker {os.getpid()})")
        return list(self._cache.values())

    def _fetch_all_rows(self) -> List[tuple]:
        """Internal sync method to fetch rows"""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT id, raw_data FROM {self.table}")
                return cur.fetchall()
        finally:
            self._release_conn(conn)

    async def get_token(self, token_id: str) -> Optional[TokenData]:
        """Get single TokenData from cache"""
        if not self._loaded:
            await self.load_all()
        return self._cache.get(token_id)

    async def save_all(self, tokens: List[TokenData]):
        """Save List[TokenData] to DB, storing full dict in raw_data"""
        if not tokens:
            return

        # Prepare data: convert TokenData -> dict for JSON storage
        items_to_save = []
        for t in tokens:
            full_dict = t.to_dict()  # Get dict representation
            items_to_save.append((t.id, full_dict))
            # Update cache
            self._cache[t.id] = t

        # Run DB write in thread
        await asyncio.to_thread(self._executemany_upsert, items_to_save)
        logger.info(f"💾 Saved {len(tokens)} tokens to DB + cache")

    def _executemany_upsert(self, items: List[tuple]):
        """Internal sync method for batch upsert"""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                for token_id, full_dict in items:
                    cur.execute(f"""
                        INSERT INTO {self.table} (id, raw_data, updated_at, last_used_at)
                        VALUES (%s, %s, NOW(), NOW())
                        ON CONFLICT (id) DO UPDATE SET
                            raw_data = EXCLUDED.raw_data,
                            updated_at = NOW(),
                            last_used_at = NOW()
                    """, (token_id, json.dumps(full_dict, ensure_ascii=False)))
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Error saving tokens: {e}")
            raise
        finally:
            self._release_conn(conn)

    async def clear(self):
        """Clear all tokens"""
        await asyncio.to_thread(self._clear_db)
        self._cache.clear()
        self._loaded = False
        logger.info(f"🗑 Cleared all tokens from DB + cache")

    def _clear_db(self):
        """Internal sync method to clear table"""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self.table}")
            conn.commit()
        finally:
            self._release_conn(conn)

    async def health_check(self) -> bool:
        """Check DB connectivity"""
        try:
            await asyncio.to_thread(self._health_check_sync)
            return True
        except Exception as e:
            logger.error(f"Token DB health check failed: {e}")
            return False

    def _health_check_sync(self):
        """Internal sync method for health check"""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        finally:
            self._release_conn(conn)
