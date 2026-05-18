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
    - Startup: Initialize chat state backend + asyncpg DB pool
    - Shutdown: Close HTTP client + DB pools
    Args:
        app: FastAPI application instance
    """
    logger.info("FastAPI startup: initializing chat state backend...")
    # 🔥 Init backend (handles File/Postgres/Fallback)
    await init_chat_state()

    # Show active mode
    if is_fallback_active():
        logger.warning("⚠️ ChatState is running in FALLBACK mode (File). PostgreSQL was unavailable.")
    else:
        logger.info(f"✅ ChatState backend initialized: {Config.CHAT_STATE_BACKEND.value}")

    # Initializing OpenWebUI DB (lookup only)
    logger.info("FastAPI startup: initializing asyncpg pool...")
    await init_db_pool()

    yield

    logger.info("FastAPI shutdown: cleaning up resources...")
    await http_client.aclose()

    # 🔥 Close backends
    await close_chat_state()   # Closes the State DB pool
    await close_db_pool()      # Closes the OpenWebUI DB pool

# Create FastAPI application with lifespan management
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
