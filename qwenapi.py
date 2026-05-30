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
FreeQwenApi - OpenAI-compatible proxy for Qwen Chat
====================================================
Main entry point for the FastAPI application.
Architecture:
    OpenWebUI/LiteLLM → FreeQwenApi → Qwen Chat API
Key Features:
    • OpenAI API compatibility (POST /v1/chat/completions)
    • Streaming support with proper SSE formatting
    • Persistent chat state mapping (OpenWebUI chat_id ↔ Qwen chat_id)
    • Automatic retry with exponential backoff for "chat in progress" errors
    • PostgreSQL integration with asyncpg (OpenWebUI chat lookup)
    • Token management with round-robin rotation and rate limiting
    • Comprehensive logging for debugging and monitoring
Usage:
    1. Configure environment variables in .env or config.py
    2. Run: python qwenapi.py --start-proxy --host 0.0.0.0 --port 3264
    3. Point your OpenAI-compatible client to http://<host>:3264/api
Author: Oleh Mamont et al.
License: GPLv3
"""

import os
import json
import time
import asyncio
import logging
import argparse
from datetime import datetime
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# Import our configuration module
# Config contains: HTTP settings, paths, model mappings, DB params, etc.
from config import Config, setup_logging

# Import async database functions
from db_async import init_db_pool, close_db_pool, test_db_connection

# Import chat state factory
from chat_state.factory import init_chat_state, close_chat_state, get_chat_state_backend, is_fallback_active

# Import handlers
from handlers.completions import handle_chat_completions
from chat.models import load_available_models

# Import auth browser for CLI
from auth.browser import login_interactive

# Import token factory
from token_backends.factory import init_token_storage, close_token_storage, get_token_backend

_args = None

# =================================================================
# INITIALIZATION
# =================================================================

# Ensure all required directories exist before starting the application
Config.ensure_dirs()

# Configure logging according to Config settings (level, format, file output)
# Returns a logger instance configured for this module
logger = setup_logging()

# Create a persistent HTTP client for making requests to Qwen API
# - timeout: Maximum time to wait for a response (from Config)
# - follow_redirects: Whether to automatically follow HTTP redirects
http_client = httpx.AsyncClient(
    timeout=Config.HTTP_TIMEOUT,
    follow_redirects=Config.HTTP_FOLLOW_REDIRECTS
)

# Inject http_client into modules that need it
from core.engine import set_http_client as set_engine_http_client
from chat.mapping import set_http_client as set_mapping_http_client

set_engine_http_client(http_client)
set_mapping_http_client(http_client)

# =================================================================
# FASTAPI APP with LIFESPAN
# =================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan handler: manage startup/shutdown lifecycle.
    - Startup: Auto-migration + Init backends + DB pools
    - Shutdown: Close all resources
    """
    # 🔧 STEP 1: Embeded postgre migration
    if getattr(Config, "ENABLE_AUTO_MIGRATION", True):
        logger.info("🔧 Running inline database migration...")
        try:
            import asyncpg
            import json
            from pathlib import Path

            async def _run_migration_for(db_cfg: dict, json_file: Path, table: str, name: str, is_tokens: bool = False):
                """Universal migration function with debug logs."""
                if not json_file.exists():
                    logger.warning(f"⚠️ {json_file.name} NOT FOUND at {json_file}")
                    return
                
                logger.info(f"🚀 Migrating {name} from {json_file}...")
                
                # Connecting to the database
                conn = await asyncpg.connect(
                    host=db_cfg["host"], port=db_cfg.get("port", 5432),
                    database=db_cfg["db"], user=db_cfg["user"], password=db_cfg["pass"]
                )
                try:
                    # 1. Create a scheme (Chat State only)
                    if not is_tokens:
                        # Create table if not exists
                        await conn.execute(f"""
                            CREATE TABLE IF NOT EXISTS {table} (
                                openweb_id TEXT NOT NULL, qwen_chat_id TEXT NOT NULL,
                                last_parent_id TEXT, is_new BOOLEAN DEFAULT FALSE, 
                                created_at DOUBLE PRECISION, updated_at TIMESTAMP DEFAULT NOW(),
                                PRIMARY KEY (openweb_id)
                            )
                        """)
                        
                        # 🔥 CHECK: Is there a 'model' column? If not, add it.
                        column_exists = await conn.fetchval("""
                            SELECT 1 FROM information_schema.columns 
                            WHERE table_schema = 'public' AND table_name = $1 AND column_name = 'model'
                        """, table)
                        
                        if not column_exists:
                            logger.info(f"🔧 Adding 'model' column to {table}...")
                            try:
                                await conn.execute(f"ALTER TABLE {table} ADD COLUMN model TEXT DEFAULT ''")
                            except asyncpg.exceptions.DuplicateColumnError:
                                # Another worker has already added a column - this is a normal race
                                logger.debug(f"⚡ Column 'model' already exists in {table} (parallel run, safe)")
                            except Exception as e:
                                # Any other error is a real problem.
                                logger.error(f"❌ Failed to add 'model' column: {e}")
                                raise
                        
                        # 🔥 CHECK: Do I need to change the PK to a composite one?
                        pk_info = await conn.fetchrow("""
                            SELECT string_agg(column_name, ',' ORDER BY ordinal_position) as pk_cols,
                                   constraint_name
                            FROM information_schema.key_column_usage
                            WHERE table_schema = 'public' AND table_name = $1 
                            AND constraint_name LIKE '%_pkey'
                            GROUP BY constraint_name
                        """, table)
                        
                        if pk_info and pk_info['pk_cols'] == 'openweb_id':
                            logger.info(f"🔧 Migrating PK on {table} to composite (openweb_id, model)...")
                            try:
                                # Normalize existing NULLs in the model before changing the key
                                await conn.execute(f"UPDATE {table} SET model = '' WHERE model IS NULL")
                                
                                # We are trying to recreate the PK
                                constraint_name = pk_info['constraint_name']
                                await conn.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint_name}")
                                await conn.execute(f"ALTER TABLE {table} ADD PRIMARY KEY (openweb_id, model)")
                                
                            except asyncpg.exceptions.UndefinedObjectError as e:
                                # 🔥 MOST IMPORTANT: If the restriction has already been removed by another worker, it's OK
                                # We check the error text for the presence of "does not exist" or "does not exist" markers.
                                err_msg = str(e).lower()
                                if "не существует" in err_msg or "does not exist" in err_msg:
                                    logger.debug(f"⚡ PK constraint '{constraint_name}' already dropped by another worker (safe race condition)")
                                else:
                                    # If the error is different, we actually drop it.
                                    raise
                                    
                            except asyncpg.exceptions.InvalidTableDefinitionError:
                                # PK has already been changed to a composite by another worker - this is also OK
                                logger.debug(f"⚡ PK already migrated to composite by another worker (safe race condition)")
                                
                            except Exception as e:
                                # Any other unexpected error
                                logger.error(f"❌ Failed to migrate PK: {e}")
                                raise
                        
                        # Index checking (race protection)
                        index_exists = await conn.fetchval("""
                            SELECT 1 FROM pg_indexes 
                            WHERE tablename = $1 AND indexname = $2
                        """, table, f"idx_{table}_upd")
                        
                        if not index_exists:
                            try:
                                await conn.execute(f"CREATE INDEX idx_{table}_upd ON {table}(updated_at)")
                            except asyncpg.exceptions.UniqueViolationError:
                                pass
                    else:
                        # Token scheme
                        await conn.execute(f"""
                            CREATE TABLE IF NOT EXISTS {table} (
                                id TEXT PRIMARY KEY, raw_data JSONB NOT NULL,
                                created_at TIMESTAMPTZ DEFAULT NOW(),
                                updated_at TIMESTAMPTZ DEFAULT NOW(),
                                last_used_at TIMESTAMPTZ DEFAULT NOW()
                            )
                        """)
                    
                    # 2. Loading and DEBUG format
                    with open(json_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    
                    logger.debug(f"🔍 {name}: JSON type={type(data).__name__}, len={len(data) if hasattr(data, '__len__') else 'N/A'}")
                    if isinstance(data, dict) and data:
                        first_key, first_val = next(iter(data.items()))
                        logger.debug(f"🔍 {name}: Sample key='{first_key[:10]}...', val_type={type(first_val).__name__}, val_keys={list(first_val.keys()) if isinstance(first_val, dict) else 'N/A'}")
                    elif isinstance(data, list) and data:
                        logger.debug(f"🔍 {name}: Sample item keys={list(data[0].keys()) if isinstance(data[0], dict) else 'N/A'}")
                    
                    # 3. Record parsing (universal + debug)
                    records_to_migrate = []
                    
                    if isinstance(data, dict):
                        for key, val in data.items():
                            records_to_migrate.append((key, val))
                    elif isinstance(data, list):
                        for item in data:
                            if isinstance(item, dict):
                                record_id = item.get("openweb_id") or item.get("id")
                                if record_id:
                                    records_to_migrate.append((record_id, item))
                    else:
                        logger.error(f"❌ {name}: Unknown JSON format {type(data)}")
                        return

                    logger.info(f"📦 {name}: Parsed {len(records_to_migrate)} potential records")
                    
                    # 🔥 DEBUG: We'll show the first 3 entries to understand the format.
                    for i, (rid, rval) in enumerate(records_to_migrate[:3]):
                        logger.debug(f"🔍 {name}[{i}]: id='{rid[:10]}...', type={type(rval).__name__}, content={str(rval)[:200]}")

                    # 4. UPSERT loop
                    count = 0
                    skipped = 0
                    for record_id, record_val in records_to_migrate:
                        try:
                            if not is_tokens:
                                # === Chat State Logic ===
                                qwen_id = None
                                
                                # 🔥 Universal extraction of qwen_chat_id from any format
                                if isinstance(record_val, str):
                                    # Format 1: Just a string with an ID
                                    qwen_id = record_val
                                elif isinstance(record_val, dict):
                                    # Format 2: Dictionary with qwen_chat_id field
                                    qwen_id = record_val.get("qwen_chat_id")
                                    # Format 3: A dictionary where the value itself is an ID (legacy)
                                    if not qwen_id and len(record_val) == 1:
                                        qwen_id = next(iter(record_val.values()), None)
                                    # Format 4: The field is simply called "chat_id"
                                    if not qwen_id:
                                        qwen_id = record_val.get("chat_id")
                                
                                if not qwen_id:
                                    skipped += 1
                                    if skipped <= 3:
                                        logger.warning(f"⚠️ {name}: Skipping {record_id[:10]}... (no qwen_chat_id found, val={record_val})")
                                    continue
                                    
                                lp_id = None
                                is_new = False
                                created = 0.0
                                model = ""
                                
                                if isinstance(record_val, dict):
                                    lp_id = record_val.get("last_parent_id")
                                    is_new = record_val.get("is_new", False)
                                    created = record_val.get("created_at", 0.0)
                                    model = record_val.get("model", "") or ""
                                
                                await conn.execute(f"""
                                    INSERT INTO {table} (openweb_id, model, qwen_chat_id, last_parent_id, is_new, created_at, updated_at)
                                    VALUES ($1, $2, $3, $4, $5, $6, NOW())
                                    ON CONFLICT (openweb_id, model) DO UPDATE SET
                                        qwen_chat_id = EXCLUDED.qwen_chat_id, last_parent_id = EXCLUDED.last_parent_id,
                                        is_new = EXCLUDED.is_new, created_at = EXCLUDED.created_at, updated_at = NOW()
                                """, record_id, model, qwen_id, lp_id, is_new, created)
                            
                            else:
                                # === Tokens Logic ===
                                await conn.execute(f"""
                                    INSERT INTO {table} (id, raw_data, created_at, updated_at, last_used_at)
                                    VALUES ($1, $2, NOW(), NOW(), NOW())
                                    ON CONFLICT (id) DO UPDATE SET 
                                        raw_data = EXCLUDED.raw_data, updated_at = NOW()
                                """, record_id, json.dumps(record_val, ensure_ascii=False))
                            
                            count += 1
                        except Exception as e:
                            logger.warning(f"⚠️ {name}: Error processing {record_id}: {e}")
                            skipped += 1
                    
                    # 🔥 FINAL REPORT
                    logger.info(f"✅ {name}: {count} migrated, {skipped} skipped.")

                finally:
                    await conn.close()

            # === LAUNCHING MIGRATIONS ===
            
            # 1. Chat State
            await _run_migration_for({
                "host": Config.CHAT_STATE_DB_HOST, "port": Config.CHAT_STATE_DB_PORT,
                "db": Config.CHAT_STATE_DB_NAME, "user": Config.CHAT_STATE_DB_USER, "pass": Config.CHAT_STATE_DB_PASSWORD
            }, Config.SESSION_DIR / "chat_state.json", Config.CHAT_STATE_DB_TABLE, "Chat State", is_tokens=False)

            # 2. Tokens
            await _run_migration_for({
                "host": Config.TOKEN_DB_HOST, "port": Config.TOKEN_DB_PORT,
                "db": Config.TOKEN_DB_NAME, "user": Config.TOKEN_DB_USER, "pass": Config.TOKEN_DB_PASSWORD
            }, Config.SESSION_DIR / "tokens.json", Config.TOKEN_DB_TABLE, "Tokens", is_tokens=True)

            logger.info("✅ Inline migration completed successfully.")
            
        except Exception as e:
            logger.error(f"❌ Migration failed: {e}")
            logger.warning("⚠️ App starting. Verify DB state if errors persist.")

    # 🔧 STEP 2: Initialize backends
    logger.info("FastAPI startup: initializing backends...")
    await init_chat_state()
    await init_token_storage()

    if is_fallback_active():
        logger.warning("⚠️ ChatState is running in FALLBACK mode (File). PostgreSQL was unavailable.")
    else:
        logger.info(f"✅ ChatState backend initialized: {Config.CHAT_STATE_BACKEND.value}")

    # Initializing OpenWebUI DB (lookup only)
    logger.info("FastAPI startup: initializing asyncpg pool...")
    await init_db_pool()

    # Processing --clear-tokens
    if _args and _args.clear_tokens:
        try:
            backend = get_token_backend()
            await backend.clear()
            logger.info("🧹 Tokens cleared on startup via --clear-tokens")
        except Exception as e:
            logger.warning(f"Clear tokens failed: {e}")

    yield  # 🔥 App is running and serving requests

    # 🔧 SHUTDOWN: Cleanup resources
    logger.info("FastAPI shutdown: cleaning up resources...")
    await http_client.aclose()
    
    # 🔥 Close backends
    await close_token_storage()# Closes the Token DB pool
    await close_chat_state()   # Closes the State DB pool
    await close_db_pool()      # Closes the OpenWebUI DB pool

# 🔥 Create FastAPI application ONCE with merged lifespan
app = FastAPI(title="FreeQwenApi Python", lifespan=lifespan)

# Add CORS middleware to allow cross-origin requests (for web clients)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =================================================================
# API ROUTES
# =================================================================

@app.get("/api/chat/completions")
async def chat_completions_get():
    """Handle GET requests to /api/chat/completions (not supported)"""
    return JSONResponse(status_code=405, content={"error": "Method not supported", "message": "Use POST /api/chat/completions"})

@app.get("/api/v1/chat/completions")
async def chat_completions_v1_get():
    """Handle GET requests to /api/v1/chat/completions (not supported)"""
    return JSONResponse(status_code=405, content={"error": "Method not supported", "message": "Use POST /api/v1/chat/completions"})

@app.post("/api/chat/completions")
async def chat_completions(request: Request):
    """Handle POST requests to /api/chat/completions (main endpoint)"""
    body = await request.json()
    return await handle_chat_completions(request, body)

@app.post("/api/v1/chat/completions")
async def chat_completions_v1(request: Request):
    """Handle POST requests to /api/v1/chat/completions (OpenAI-compatible)"""
    body = await request.json()
    return await handle_chat_completions(request, body)

@app.post("/api/chat")
async def chat_endpoint(request: Request):
    """Handle POST requests to /api/chat (alternative endpoint)"""
    body = await request.json()
    return await handle_chat_completions(request, body)

@app.get("/api/models")
async def list_models():
    """Return list of available models in OpenAI-compatible format"""
    models = load_available_models()
    return {"object": "list", "data": [{"id": m, "object": "model", "created": 0, "owned_by": "qwen", "permission": []} for m in models]}

# =================================================================
# CLI MENU & LAUNCHER
# =================================================================

def print_banner():
    """Print application banner for CLI menu"""
    print(r"""   Qwen API Proxy
""")

async def interactive_menu():
    """
    Interactive CLI menu for managing the proxy.
    Provides options to:
    1. Add new authentication accounts via browser login
    2. Start the FastAPI proxy server
    3. Manage token list and chat state cache
    """
    from auth.tokens import load_tokens, save_tokens

    while True:
        os.system('clear' if os.name == 'posix' else 'cls')
        print_banner()
        tokens = load_tokens()
        print("\nAccount list:")
        if not tokens:
            print("  (empty)")
        else:
            for i, t in enumerate(tokens):
                is_limited = False
                if t.get('resetAt'):
                    is_limited = datetime.fromisoformat(t['resetAt'].replace('Z', '+00:00')).timestamp() > time.time()
                status = "Limited" if is_limited else "OK"
                print(f"  {i+1} | {t['id']} | {status}")
        print("\n=== Menu ===")
        print("1 - Add new account")
        print("2 - Re-login (not implemented)")
        print("3 - Start proxy")
        print("4 - Delete account")
        print("5 - Clear chat cache")
        print("0 - Exit")
        try:
            choice = input("\nYour choice (Enter = 3): ").strip()
        except EOFError:
            break
        if choice == "" or choice == "3":
            if not tokens:
                print("Error: Add at least one account first (item 1).")
                time.sleep(2)
                continue
            print(f"\nStarting server on {Config.HOST}:{Config.PORT}...")
            config = uvicorn.Config(app, host=Config.HOST, port=Config.PORT, log_level="info")
            server = uvicorn.Server(config)
            await server.serve()
            break
        elif choice == "1":
            print("\n--- Add account ---")
            print("1 - Manual browser login")
            print("2 - Auto login (Email + Password)")
            sub_choice = input("Choose method: ").strip()
            if sub_choice == "2":
                email = input("Email: ").strip()
                password = input("Password: ").strip()
                await login_interactive(email, password, headless=False)
            else:
                await login_interactive(headless=False)
        elif choice == "4":
            if not tokens:
                continue
            try:
                idx = int(input("Enter account number to delete: ")) - 1
                if 0 <= idx < len(tokens):
                    tokens.pop(idx)
                    save_tokens(tokens)
                    print("Account deleted.")
                    time.sleep(1)
            except ValueError:
                pass
        elif choice == "5":
            # Clear chat cache via backend
            try:
                backend = get_chat_state_backend()
                # Note: Backend might not support clear() method, so we just log
                logger.info("Chat cache clear requested. Backend state will be reset on restart.")
            except Exception as e:
                logger.warning(f"Could not access backend for clear: {e}")

            # Remove files
            if Config.CHAT_STATE_FILE.exists():
                Config.CHAT_STATE_FILE.unlink()
                logger.info(f"Deleted file {Config.CHAT_STATE_FILE}")
            if Config.CHAT_MAPPING_FILE.exists():
                Config.CHAT_MAPPING_FILE.unlink()
                logger.info(f"Deleted file {Config.CHAT_MAPPING_FILE}")

            print("Chat cache cleared.")
            time.sleep(1)

        elif choice == "0":
            break

def parse_args():
    """
    Parse command line arguments for CLI launcher.
    Supported arguments:
    --start-proxy   : Start FastAPI proxy immediately
    --login         : Start interactive Qwen auth via browser
    --clear-tokens  : Remove old/dead tokens before login
    --list-tokens   : List current tokens and exit
    --email         : Email for login (optional, with --login)
    --password      : Password for login (optional, with --login)
    --host          : Host for uvicorn (default: from Config)
    --port          : Port for uvicorn (default: from Config)
    --reload        : Enable uvicorn auto-reload (development)
    Returns:
        argparse.Namespace: Parsed arguments
    """
    parser = argparse.ArgumentParser(description="FreeQwenApi CLI Launcher")
    parser.add_argument("--start-proxy", action="store_true", help="Start FastAPI proxy immediately")
    parser.add_argument("--login", action="store_true", help="Start interactive Qwen auth via browser")
    parser.add_argument("--clear-tokens", action="store_true", help="Remove old/dead tokens before login")
    parser.add_argument("--list-tokens", action="store_true", help="List current tokens")
    parser.add_argument("--email", type=str, help="Email for login (optional)")
    parser.add_argument("--password", type=str, help="Password for login (optional)")
    parser.add_argument("--host", default=Config.HOST, help="Host for uvicorn")
    parser.add_argument("--port", default=Config.PORT, type=int, help="Port for uvicorn")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn reload")
    return parser.parse_args()

# =================================================================
# MODULE-LEVEL INIT
# =================================================================

if __name__ == "__main__":
    # Entry point when run as script
    args = parse_args()
    _args = args  # Save for lifespan
    if args.login:
        import asyncio
        asyncio.run(login_interactive(email=args.email, password=args.password, clear_existing=args.clear_tokens))
    elif args.list_tokens:
        from auth.tokens import load_tokens
        tokens = load_tokens()
        print(json.dumps(tokens, indent=2, ensure_ascii=False))
    elif args.start_proxy:
        logger.info(f"Starting FastAPI proxy on {args.host}:{args.port} ...")
        logger.info(f"Log level: {'DEBUG' if Config.DEBUG_LOGGING else 'INFO'} (DEBUG_LOGGING={Config.DEBUG_LOGGING})")
        logger.info(f"OpenWebUI DB: {'enabled' if Config.OPENWEBUI_DB_ENABLED else 'disabled'} (using asyncpg)")
        logger.info(f"Chat ID mode: {Config.OPENWEBUI_CHAT_ID_MODE}")
        # 🔥 IMPORTANT: module name is "qwenapi", not "main" for uvicorn
        uvicorn.run("qwenapi:app", host=args.host, port=args.port, reload=args.reload)
    else:
        print("No action specified. Use --help for usage.")
