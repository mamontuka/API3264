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
#

"""
Migration script: chat_state.json -> PostgreSQL Chat State DB.

Usage:
    python migrate_to_pg.py

Features:
    • Reads settings from .env file
    • Connects via Unix socket as superuser
    • Creates database, user, and table if needed
    • Migrates all records with proper format handling
    • Supports legacy and new JSON formats
    • Idempotent: safe to run multiple times
"""
import os
import sys
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any

# Add parent dir to path for imports
sys.path.insert(0, str(Path(__file__).parent.resolve()))

import asyncpg
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# =================================================================
# CONFIGURATION FROM ENV
# =================================================================
SCRIPT_DIR = Path(__file__).parent.resolve()
ENV_FILE = SCRIPT_DIR / ".env"
JSON_FILE = SCRIPT_DIR / "session" / "chat_state.json"

DB_NAME = os.getenv("CHAT_STATE_DB_NAME", "api3264_chat_state")
DB_USER = os.getenv("CHAT_STATE_DB_USER", "freeqwenapi")
DB_PASSWORD = os.getenv("CHAT_STATE_DB_PASSWORD", "freeqwenapi")
DB_TABLE = os.getenv("CHAT_STATE_DB_TABLE", "chat_mappings")
DB_SUPERUSER = os.getenv("PG_SUPERUSER", "postgres")

# Unix socket path (standard locations)
SOCKET_DIRS = ["/var/run/postgresql", "/tmp"]

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("Migration")

async def find_socket_dir() -> str:
    """Find existing PostgreSQL socket directory."""
    for d in SOCKET_DIRS:
        if os.path.isdir(d):
            return d
    raise RuntimeError(f"PostgreSQL socket directory not found. Checked: {SOCKET_DIRS}")

async def create_db_and_user(socket_dir: str) -> None:
    """Create database and user using superuser connection via socket."""
    logger.info(f"🔧 Connecting as superuser '{DB_SUPERUSER}' via socket...")

    conn = await asyncpg.connect(
        host=socket_dir,
        user=DB_SUPERUSER,
        database="postgres"
    )

    try:
        # Create user if not exists
        logger.info(f"👤 Checking user '{DB_USER}'...")
        row = await conn.fetchrow(
            "SELECT 1 FROM pg_roles WHERE rolname = $1", DB_USER
        )
        if not row:
            logger.info(f"➕ Creating user '{DB_USER}'...")
            await conn.execute(
                f"CREATE USER {DB_USER} WITH PASSWORD '{DB_PASSWORD}'"
            )
        else:
            logger.info(f"✅ User '{DB_USER}' already exists.")

        # Create database if not exists
        logger.info(f"🗄 Checking database '{DB_NAME}'...")
        row = await conn.fetchrow(
            "SELECT 1 FROM pg_database WHERE datname = $1", DB_NAME
        )
        if not row:
            logger.info(f"➕ Creating database '{DB_NAME}'...")
            await conn.execute(
                f"CREATE DATABASE {DB_NAME} OWNER {DB_USER}"
            )
        else:
            logger.info(f"✅ Database '{DB_NAME}' already exists.")

        # Grant privileges
        logger.info("🔑 Granting privileges...")
        await conn.execute(f"GRANT ALL PRIVILEGES ON DATABASE {DB_NAME} TO {DB_USER}")

    finally:
        await conn.close()

    logger.info("✅ Database and user setup complete.")


async def create_table_and_migrate(socket_dir: str) -> int:
    """Connect to target DB, create table, and migrate data."""
    logger.info(f"🔌 Connecting to '{DB_NAME}' as '{DB_USER}'...")

#    conn = await asyncpg.connect(
#        host=socket_dir,
#        database=DB_NAME,
#        user=DB_USER,
#        password=DB_PASSWORD
#    )

    conn = await asyncpg.connect(
        host='127.0.0.1',
        port=5432,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )

    try:
        # Create table
        logger.info(f"📋 Creating table '{DB_TABLE}'...")
        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE} (
                openweb_id TEXT PRIMARY KEY,
                qwen_chat_id TEXT NOT NULL,
                last_parent_id TEXT,
                is_new BOOLEAN DEFAULT FALSE,
                created_at DOUBLE PRECISION,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{DB_TABLE}_updated 
            ON {DB_TABLE}(updated_at)
        """)
        logger.info(f"✅ Table '{DB_TABLE}' ready.")

        # Load JSON file
        if not JSON_FILE.exists():
            logger.error(f"❌ JSON file not found: {JSON_FILE}")
            return 0

        logger.info(f"📂 Reading {JSON_FILE}...")
        with open(JSON_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            logger.error("❌ Invalid JSON format: expected object/dict")
            return 0

        total = len(data)
        logger.info(f"📦 Found {total} records to migrate.")

        # Migrate records
        migrated = 0
        skipped = 0
        errors = 0

        for openweb_id, value in data.items():
            try:
                # Parse record (support legacy and new formats)
                if isinstance(value, str):
                    # Legacy: "openweb_id": "qwen_chat_id"
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

                # Insert with UPSERT
                await conn.execute(f"""
                    INSERT INTO {DB_TABLE} 
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

        # Summary
        logger.info("=" * 50)
        logger.info("📊 MIGRATION SUMMARY")
        logger.info("=" * 50)
        logger.info(f"✅ Migrated: {migrated}")
        logger.info(f"⏭️ Skipped: {skipped}")
        logger.info(f"❌ Errors: {errors}")
        logger.info(f"📈 Total processed: {migrated + skipped + errors} / {total}")
        logger.info("=" * 50)

        return migrated

    finally:
        await conn.close()

async def main():
    """Main entry point."""
    logger.info("🚀 Starting migration: chat_state.json → PostgreSQL")
    logger.info(f"📁 Script dir: {SCRIPT_DIR}")
    logger.info(f"🔧 DB Name: {DB_NAME}")
    logger.info(f"👤 DB User: {DB_USER}")
    logger.info(f"📋 DB Table: {DB_TABLE}")

    # Check JSON file
    if not JSON_FILE.exists():
        logger.error(f"❌ JSON file not found: {JSON_FILE}")
        logger.error("💡 Make sure session/chat_state.json exists.")
        sys.exit(1)

    try:
        # Find socket
        socket_dir = await find_socket_dir()
        logger.info(f"🔌 Using socket directory: {socket_dir}")

        # Step 1: Create DB and user
        await create_db_and_user(socket_dir)

        # Step 2: Create table and migrate
        migrated = await create_table_and_migrate(socket_dir)

        if migrated > 0:
            logger.info("🎉 Migration completed successfully!")
            logger.info(f"💡 You can now set CHAT_STATE_BACKEND=postgres in .env")
        else:
            logger.warning("⚠️ No records were migrated. Check logs for details.")

    except Exception as e:
        logger.error(f"💀 Migration failed: {type(e).__name__}: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
