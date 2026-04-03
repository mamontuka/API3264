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
# along with this program. If not, see <https://www.gnu.org>.
#
#

"""
Configuration module for FreeQwenApi Proxy
All settings are loaded from environment variables with sensible defaults.
"""
import os
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any
from dotenv import load_dotenv

# Загружаем переменные окружения из .env
load_dotenv()

# =================================================================
# BASE CONFIGURATION
# =================================================================
class Config:
    """Main configuration class with environment variable support"""

    # 🔥 Server settings
    PORT: int = int(os.getenv("PORT", "3264"))
    HOST: str = os.getenv("HOST", "10.32.64.2")

    # 🔥 Qwen API settings
    QWEN_BASE_URL: str = os.getenv("QWEN_BASE_URL", "https://chat.qwen.ai")
    CHAT_PAGE_URL: str = f"{QWEN_BASE_URL}/"
    CHAT_API_URL: str = f"{QWEN_BASE_URL}/api/v2/chat/completions"
    CREATE_CHAT_URL: str = f"{QWEN_BASE_URL}/api/v2/chats/new"

    # 🔥 File paths (absolute, relative to this file)
    SCRIPT_DIR: Path = Path(__file__).parent.resolve()
    SESSION_DIR: Path = SCRIPT_DIR / "session"
    TOKENS_FILE: Path = SESSION_DIR / "tokens.json"
    CHAT_STATE_FILE: Path = SESSION_DIR / "chat_state.json"
    CHAT_MAPPING_FILE: Path = SESSION_DIR / "chat_mapping.json"
    AVAILABLE_MODELS_FILE: Path = SCRIPT_DIR / "AvailableModels.txt"

    # 🔥 Default model and mapping
    DEFAULT_MODEL: str = os.getenv("DEFAULT_MODEL", "qwen-max-latest")
    MODEL_MAPPING: Dict[str, str] = {
        "qwen3.5": "qwen3.5-plus",
        "qwen-max": "qwen3-max",
        "qwen-vl": "qwen3-vl-plus",
        "qwen-coder": "qwen3-coder-plus",
        "qwen3": "qwen3-235b-a22b",
        "qwq": "qwq-32b",
        "qvq": "qvq-72b-preview-0310",
    }

    # 🔥 Logging configuration
    DEBUG_LOGGING: bool = os.getenv("DEBUG_LOGGING", "false").lower() in ("true", "1", "yes", "on")
    LOG_LEVEL: int = logging.DEBUG if DEBUG_LOGGING else logging.INFO
    LOG_FORMAT: str = '[%(asctime)s] [%(levelname)s] %(message)s'
    LOG_DATEFMT: str = '%H:%M:%S'

    # 🔥 OpenWebUI integration
    OPENWEBUI_CHAT_ID_MODE: str = os.getenv("OPENWEBUI_CHAT_ID_MODE", "stable").lower()  # stable, per_request, smart
    OPENWEBUI_USER_ID_HEADER: str = os.getenv("OPENWEBUI_USER_ID_HEADER", "x-openwebui-user-id")

    # 🔥 PostgreSQL for OpenWebUI (optional)
    OPENWEBUI_DB_ENABLED: bool = os.getenv("OPENWEBUI_DB_ENABLED", "false").lower() in ("true", "1", "yes", "on")
    OPENWEBUI_DB_HOST: str = os.getenv("OPENWEBUI_DB_HOST", "localhost")
    OPENWEBUI_DB_PORT: int = int(os.getenv("OPENWEBUI_DB_PORT", "5432"))
    OPENWEBUI_DB_NAME: str = os.getenv("OPENWEBUI_DB_NAME", "openwebui")
    OPENWEBUI_DB_USER: str = os.getenv("OPENWEBUI_DB_USER", "openwebui")
    OPENWEBUI_DB_PASSWORD: str = os.getenv("OPENWEBUI_DB_PASSWORD", "")
    OPENWEBUI_DB_SSL_MODE: str = os.getenv("OPENWEBUI_DB_SSL_MODE", "prefer")
    OPENWEBUI_DB_CONNECT_TIMEOUT: int = int(os.getenv("OPENWEBUI_DB_CONNECT_TIMEOUT", "5"))

    # 🔥 Browser auth settings
    CHROME_USER_DATA: str = os.getenv("CHROME_USER_DATA", str(SCRIPT_DIR / "profile"))

    # 🔥 HTTP client settings
    HTTP_TIMEOUT: float = float(os.getenv("HTTP_TIMEOUT", "900.0"))
    HTTP_FOLLOW_REDIRECTS: bool = os.getenv("HTTP_FOLLOW_REDIRECTS", "true").lower() in ("true", "1", "yes", "on")

    # 🔥 Default headers for Qwen API requests
    DEFAULT_HEADERS: Dict[str, str] = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": QWEN_BASE_URL,
        "Referer": CHAT_PAGE_URL,
    }

    @classmethod
    def ensure_dirs(cls):
        """Ensure all required directories exist"""
        cls.SESSION_DIR.mkdir(parents=True, exist_ok=True)
        Path(cls.CHROME_USER_DATA).mkdir(parents=True, exist_ok=True)

    @classmethod
    def get_chat_id_headers(cls) -> List[str]:
        """List of header names that may contain chat/conversation ID"""
        return [
            "x-chat-id", "x-conversation-id", "openwebui-chat-id",
            "x-openwebui-chat-id", "chatid", "conversationid"
        ]

    @classmethod
    def get_chat_id_fields(cls) -> List[str]:
        """List of JSON body field names that may contain chat/conversation ID"""
        return [
            "chatId", "chat_id", "conversation_id", "conversationId",
            "thread_id", "threadId", "session_id", "sessionId"
        ]

    @classmethod
    def get_nested_chat_id_paths(cls) -> List[tuple]:
        """List of (parent_key, child_key) tuples for nested chat ID lookup"""
        return [
            ("metadata", "chat_id"),
            ("metadata", "conversation_id"),
            ("kwargs", "chat_id"),
            ("extra_body", "chat_id"),
            ("extra_body", "conversation_id"),
        ]


# =================================================================
# LOGGING SETUP (executed at import time)
# =================================================================
def setup_logging():
    """Configure logging based on Config settings"""
    # Clear existing handlers to avoid duplicates
    root_logger = logging.getLogger()
    if root_logger.handlers:
        root_logger.handlers.clear()

    # Configure root logger
    logging.basicConfig(
        level=Config.LOG_LEVEL,
        format=Config.LOG_FORMAT,
        datefmt=Config.LOG_DATEFMT,
        force=True  # Clear existing handlers
    )

    # Set levels for noisy libraries
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("fastapi").setLevel(logging.WARNING)

    # Optional: psycopg2 logging if DB enabled
    if Config.OPENWEBUI_DB_ENABLED:
        try:
            import psycopg2
            logging.getLogger("psycopg2").setLevel(logging.WARNING)
        except ImportError:
            pass

    return logging.getLogger("FreeQwenApi")


# =================================================================
# DATABASE CONNECTION CACHE (for OpenWebUI PostgreSQL)
# =================================================================
_pg_connection_cache: Dict[str, Any] = {}

def get_pg_connection() -> Optional[Any]:
    """
    Get cached PostgreSQL connection for OpenWebUI database.
    
    🔥 FIX: Automatically recovers from aborted transactions by calling rollback().
    This prevents "current transaction is aborted" errors.
    """
    if not Config.OPENWEBUI_DB_ENABLED:
        return None

    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except ImportError:
        logging.getLogger("FreeQwenApi").warning("psycopg2 not installed. PostgreSQL support disabled.")
        return None

    cache_key = f"{Config.OPENWEBUI_DB_HOST}:{Config.OPENWEBUI_DB_PORT}:{Config.OPENWEBUI_DB_NAME}"

    # Check cache
    if cache_key in _pg_connection_cache:
        conn = _pg_connection_cache[cache_key]
        
        # Check if connection is still alive
        if conn.closed != 0:
            # Connection is closed, remove from cache
            del _pg_connection_cache[cache_key]
        else:
            # 🔥 FIX: Check for aborted transaction and recover
            try:
                transaction_status = conn.get_transaction_status()
                if transaction_status == psycopg2.extensions.TRANSACTION_STATUS_INERROR:
                    # Transaction is aborted, rollback to recover
                    conn.rollback()
                    logging.getLogger("FreeQwenApi").debug("🗄 Rolled back aborted transaction")
                return conn
            except Exception as e:
                # If we can't check status, connection might be broken
                logging.getLogger("FreeQwenApi").debug(f"⚠️ Connection check failed: {e}, recreating...")
                try:
                    conn.close()
                except:
                    pass
                del _pg_connection_cache[cache_key]

    # Create new connection
    try:
        conn = psycopg2.connect(
            host=Config.OPENWEBUI_DB_HOST,
            port=Config.OPENWEBUI_DB_PORT,
            dbname=Config.OPENWEBUI_DB_NAME,
            user=Config.OPENWEBUI_DB_USER,
            password=Config.OPENWEBUI_DB_PASSWORD,
            sslmode=Config.OPENWEBUI_DB_SSL_MODE,
            connect_timeout=Config.OPENWEBUI_DB_CONNECT_TIMEOUT
        )
        _pg_connection_cache[cache_key] = conn
        logging.getLogger("FreeQwenApi").debug(f"🗄 New DB connection established: {cache_key}")
        return conn
    except Exception as e:
        logging.getLogger("FreeQwenApi").warning(f"Failed to connect to OpenWebUI DB: {e}")
        return None


def close_all_pg_connections():
    """Close all cached PostgreSQL connections"""
    for conn in _pg_connection_cache.values():
        try:
            if conn and conn.closed == 0:
                conn.close()
        except:
            pass
    _pg_connection_cache.clear()
    logging.getLogger("FreeQwenApi").debug("🗄 All DB connections closed")
