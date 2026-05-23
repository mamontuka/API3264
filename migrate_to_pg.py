#!/usr/bin/env python3
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
"""
Universal Migration Script: chat_state.json & tokens.json → PostgreSQL.

Usage:
    python migrate_to_pg.py

Features:
    • Auto-detects chat_state.json and tokens.json
    • Migrates both to their respective PostgreSQL databases
    • Creates databases, users, tables automatically
    • Supports legacy and new JSON formats
    • Idempotent: safe to run multiple times
    • Secure: uses parameterized queries, no passwords in CLI args
"""
import os
import sys
import json
import asyncio
import logging
from pathlib import Path
from typing import Optional, List
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import asyncpg
from dotenv import load_dotenv

load_dotenv()

# =================================================================
# CONFIGURATION
# =================================================================
SCRIPT_DIR = Path(__file__).parent.resolve()

# Paths
CHAT_STATE_JSON = SCRIPT_DIR / "session" / "chat_state.json"
TOKENS_JSON = SCRIPT_DIR / "session" / "tokens.json"

# Chat State DB config
CS_DB_NAME = os.getenv("CHAT_STATE_DB_NAME", "api3264_chat_state")
CS_DB_USER = os.getenv("CHAT_STATE_DB_USER", "freeqwenapi")
CS_DB_PASSWORD = os.getenv("CHAT_STATE_DB_PASSWORD", "freeqwenapi")
CS_DB_TABLE = os.getenv("CHAT_STATE_DB_TABLE", "chat_mappings")

# Tokens DB config
TK_DB_NAME = os.getenv("TOKEN_DB_NAME", "api3264_tokens")
TK_DB_USER = os.getenv("TOKEN_DB_USER", "freeqwenapi")
TK_DB_PASSWORD = os.getenv("TOKEN_DB_PASSWORD", "freeqwenapi")
TK_DB_TABLE = os.getenv("TOKEN_DB_TABLE", "tokens")

DB_SUPERUSER = os.getenv("PG_SUPERUSER", "postgres")
SOCKET_DIRS = ["/var/run/postgresql", "/tmp"]

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("Migration")


@dataclass
class MigrationTarget:
    """Configuration for a single migration target."""
    name: str
    json_path: Path
    db_name: str
    db_user: str
    db_password: str
    db_table: str
    description: str


async def find_socket_dir() -> Optional[str]:
    """Find existing PostgreSQL socket directory."""
    for d in SOCKET_DIRS:
        if os.path.isdir(d):
            return d
    return None


async def connect_superuser(socket_dir: Optional[str]) -> asyncpg.Connection:
    """Connect as superuser via socket or TCP with fallback."""
    errors = []

    # Try socket first
    if socket_dir:
        try:
            logger.info(f"🔌 Trying superuser connection via socket: {socket_dir}")
            return await asyncpg.connect(
                host=socket_dir,
                user=DB_SUPERUSER,
                database="postgres"
            )
        except Exception as e:
            errors.append(f"socket: {e}")
            logger.debug(f"Socket connection failed: {e}")

    # Fallback to TCP
    try:
        logger.info("🔌 Trying superuser connection via TCP: 127.0.0.1")
        return await asyncpg.connect(
            host="127.0.0.1",
            port=5432,
            user=DB_SUPERUSER,
            database="postgres"
        )
    except Exception as e:
        errors.append(f"tcp: {e}")

    raise RuntimeError(
        f"Failed to connect as superuser '{DB_SUPERUSER}'. Errors: {'; '.join(errors)}\n"
        f"💡 Ensure PostgreSQL is running and superuser access is configured."
    )


async def create_db_and_user(conn: asyncpg.Connection, target: MigrationTarget) -> bool:
    """Create database and user for a specific migration target."""
    logger.info(f"🔧 Setting up {target.description}...")

    try:
        # Check/create user
        row = await conn.fetchrow(
            "SELECT 1 FROM pg_roles WHERE rolname = $1", target.db_user
        )
        if not row:
            logger.info(f"➕ Creating user '{target.db_user}'...")
            await conn.execute(
                "CREATE USER {} WITH PASSWORD $1".format(target.db_user),
                target.db_password
            )
        else:
            logger.info(f"✅ User '{target.db_user}' already exists.")

        # Check/create database
        row = await conn.fetchrow(
            "SELECT 1 FROM pg_database WHERE datname = $1", target.db_name
        )
        if not row:
            logger.info(f"➕ Creating database '{target.db_name}'...")
            await conn.execute(
                "CREATE DATABASE {} OWNER {}".format(target.db_name, target.db_user)
            )
        else:
            logger.info(f"✅ Database '{target.db_name}' already exists.")

        # Grant privileges
        logger.info(f"🔑 Granting privileges on '{target.db_name}'...")
        await conn.execute(
            "GRANT ALL PRIVILEGES ON DATABASE {} TO {}".format(target.db_name, target.db_user)
        )

        return True

    except Exception as e:
        logger.error(f"❌ Failed to setup {target.description}: {e}")
        return False


async def connect_target(target: MigrationTarget, socket_dir: Optional[str]) -> asyncpg.Connection:
    """Connect to target database with socket/TCP fallback."""
    # Try socket
    if socket_dir:
        try:
            return await asyncpg.connect(
                host=socket_dir,
                database=target.db_name,
                user=target.db_user,
                password=target.db_password
            )
        except Exception:
            pass

    # Fallback to TCP
    return await asyncpg.connect(
        host="127.0.0.1",
        port=5432,
        database=target.db_name,
        user=target.db_user,
        password=target.db_password
    )


async def migrate_chat_state(target: MigrationTarget, socket_dir: Optional[str]) -> int:
    """Migrate chat_state.json to PostgreSQL."""
    logger.info(f"🚀 Migrating {target.description}...")

    try:
        conn = await connect_target(target, socket_dir)
    except Exception as e:
        logger.error(f"❌ Cannot connect to {target.db_name}: {e}")
        return 0

    try:
        # Create table
        logger.info(f"📋 Creating table '{target.db_table}'...")
        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {target.db_table} (
                openweb_id TEXT PRIMARY KEY,
                qwen_chat_id TEXT NOT NULL,
                last_parent_id TEXT,
                is_new BOOLEAN DEFAULT FALSE,
                created_at DOUBLE PRECISION,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{target.db_table}_updated
            ON {target.db_table}(updated_at)
        """)

        # Load JSON
        with open(target.json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            logger.error("❌ Invalid JSON format: expected object/dict")
            return 0

        total = len(data)
        logger.info(f"📦 Found {total} records.")

        migrated = skipped = errors = 0

        for openweb_id, value in data.items():
            try:
                if isinstance(value, str):
                    # Legacy format
                    qwen_chat_id = value
                    last_parent_id = None
                    is_new = False
                    created_at = 0.0
                elif isinstance(value, dict):
                    qwen_chat_id = value.get("qwen_chat_id")
                    if not qwen_chat_id:
                        logger.warning(f"⚠️ Skipping {openweb_id}: missing qwen_chat_id")
                        skipped += 1
                        continue
                    last_parent_id = value.get("last_parent_id")
                    is_new = value.get("_is_new", value.get("is_new", False))
                    created_at = value.get("_created_at", value.get("created_at", 0.0))
                else:
                    logger.warning(f"⚠️ Skipping {openweb_id}: unknown format")
                    skipped += 1
                    continue

                await conn.execute(f"""
                    INSERT INTO {target.db_table}
                    (openweb_id, qwen_chat_id, last_parent_id, is_new, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, NOW())
                    ON CONFLICT (openweb_id) DO UPDATE SET
                        qwen_chat_id = EXCLUDED.qwen_chat_id,
                        last_parent_id = EXCLUDED.last_parent_id,
                        is_new = EXCLUDED.is_new,
                        created_at = EXCLUDED.created_at,
                        updated_at = NOW()
                """, openweb_id, qwen_chat_id, last_parent_id, is_new, created_at)

                migrated += 1
            except Exception as e:
                logger.error(f"❌ Error migrating {openweb_id}: {e}")
                errors += 1

        logger.info(f"✅ {target.description}: {migrated} migrated, {skipped} skipped, {errors} errors.")
        return migrated

    finally:
        await conn.close()


async def migrate_tokens(target: MigrationTarget, socket_dir: Optional[str]) -> int:
    """Migrate tokens.json to PostgreSQL - FULL MIRROR MODE"""
    logger.info(f"🚀 Migrating {target.description}...")

    try:
        conn = await connect_target(target, socket_dir)
    except Exception as e:
        logger.error(f"❌ Cannot connect to {target.db_name}: {e}")
        return 0

    try:
        # ✅ Создаем таблицу с raw_data JSONB для ПОЛНОГО хранения объекта
        logger.info(f"📋 Creating table '{target.db_table}'...")
        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {target.db_table} (
                id TEXT PRIMARY KEY,
                raw_data JSONB NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                last_used_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{target.db_table}_updated 
            ON {target.db_table}(updated_at)
        """)

        # Load JSON
        with open(target.json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        tokens_to_migrate = []

        if isinstance(data, list):
            logger.info("📦 Detected array format in tokens.json")
            for item in data:
                if isinstance(item, dict) and "id" in item:
                    token_id = item["id"]
                    # ✅ Сохраняем ВЕСЬ объект целиком в raw_data
                    tokens_to_migrate.append((token_id, item))
                else:
                    logger.warning(f"⚠️ Skipping invalid token item: missing 'id' field")
        elif isinstance(data, dict):
            logger.info("📦 Detected dict format in tokens.json")
            for token_id, token_data in data.items():
                if isinstance(token_data, dict):
                    tokens_to_migrate.append((token_id, token_data))
                else:
                    logger.warning(f"⚠️ Skipping token {token_id}: invalid data format")
        else:
            logger.error("❌ Invalid JSON format: expected array or object/dict")
            return 0

        total = len(tokens_to_migrate)
        logger.info(f"📦 Found {total} tokens to migrate.")

        migrated = skipped = errors = 0

        for token_id, full_token_obj in tokens_to_migrate:
            try:
                # ✅ Вставляем id и ВЕСЬ объект в raw_data
                await conn.execute(f"""
                    INSERT INTO {target.db_table}
                    (id, raw_data, created_at, updated_at, last_used_at)
                    VALUES ($1, $2, NOW(), NOW(), NOW())
                    ON CONFLICT (id) DO UPDATE SET
                        raw_data = EXCLUDED.raw_data,
                        updated_at = NOW(),
                        last_used_at = NOW()
                """, token_id, json.dumps(full_token_obj, ensure_ascii=False))

                migrated += 1
            except Exception as e:
                logger.error(f"❌ Error migrating token {token_id}: {e}")
                errors += 1

        logger.info(f"✅ {target.description}: {migrated} migrated, {skipped} skipped, {errors} errors.")
        return migrated

    finally:
        await conn.close()


async def main():
    logger.info("🚀 Universal Migration Tool: JSON → PostgreSQL")
    logger.info(f"📁 Script dir: {SCRIPT_DIR}")

    targets: List[MigrationTarget] = []

    # Detect chat_state
    if CHAT_STATE_JSON.exists():
        targets.append(MigrationTarget(
            name="chat_state",
            json_path=CHAT_STATE_JSON,
            db_name=CS_DB_NAME,
            db_user=CS_DB_USER,
            db_password=CS_DB_PASSWORD,
            db_table=CS_DB_TABLE,
            description="Chat State"
        ))
        logger.info(f"📂 Found {CHAT_STATE_JSON}")
    else:
        logger.info(f"ℹ️  {CHAT_STATE_JSON} not found, skipping chat state migration.")

    # Detect tokens
    if TOKENS_JSON.exists():
        targets.append(MigrationTarget(
            name="tokens",
            json_path=TOKENS_JSON,
            db_name=TK_DB_NAME,
            db_user=TK_DB_USER,
            db_password=TK_DB_PASSWORD,
            db_table=TK_DB_TABLE,
            description="Tokens"
        ))
        logger.info(f"📂 Found {TOKENS_JSON}")
    else:
        logger.info(f"ℹ️  {TOKENS_JSON} not found, skipping tokens migration.")

    if not targets:
        logger.error("❌ No JSON files found to migrate.")
        logger.error(f"💡 Ensure {CHAT_STATE_JSON} or {TOKENS_JSON} exist.")
        sys.exit(1)

    # Find socket
    socket_dir = await find_socket_dir()
    if socket_dir:
        logger.info(f"🔌 Using socket directory: {socket_dir}")
    else:
        logger.warning("⚠️ Socket directory not found, will use TCP connection.")

    try:
        # Step 1: Create DBs and users via superuser
        super_conn = await connect_superuser(socket_dir)
        try:
            for target in targets:
                await create_db_and_user(super_conn, target)
        finally:
            await super_conn.close()

        # Step 2: Migrate data
        total_migrated = 0
        for target in targets:
            if target.name == "chat_state":
                total_migrated += await migrate_chat_state(target, socket_dir)
            elif target.name == "tokens":
                total_migrated += await migrate_tokens(target, socket_dir)

        # Summary
        logger.info("=" * 60)
        logger.info("🎉 MIGRATION COMPLETE")
        logger.info("=" * 60)
        logger.info(f"📊 Total records migrated: {total_migrated}")
        logger.info("💡 Update your .env:")
        logger.info("   CHAT_STATE_BACKEND=postgres")
        logger.info("   TOKEN_STORAGE_BACKEND=postgres")
        logger.info("=" * 60)

    except Exception as e:
        logger.error(f"💀 Migration failed: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
