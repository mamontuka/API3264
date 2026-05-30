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
Configuration module for FreeQwenApi Proxy
All settings are loaded from environment variables with sensible defaults.
"""
import os
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any
from enum import Enum
from dotenv import load_dotenv

# Load variables from .env
load_dotenv()

# =================================================================
# ENUMS & CONSTANTS
# =================================================================

class ChatStateBackendType(str, Enum):
    FILE = "file"
    POSTGRES = "postgres"

class TokenBackendType(str, Enum):
    FILE = "file"
    POSTGRES = "postgres"

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
    DEFAULT_MODEL: str = os.getenv("DEFAULT_MODEL", "qwen3.5-max")
    MODEL_MAPPING: Dict[str, str] = {
        "qwen3.7-max": "qwen3.7-max",
        "qwen3.7-plus-preview": "qwen-latest-series-invite-beta-v16",
        "qwen3.7-max-preview": "qwen-latest-series-invite-beta-v24",
        "qwen3.6": "qwen3.6-plus",
        "qwen3.6-max": "qwen3.6-max-preview",
        "qwen3.5": "qwen3.5-plus",
        "qwen3.5-max": "qwen3.5-max-2026-03-08",
        "qwen-vl": "qwen3-vl-235b-a22b",
        "qwen3-coder": "qwen3-coder-plus",
        "qwen3": "qwen3-235b-a22b",
        "qwen2": "qwen2.5-max",
    }
    # =================================================================
    # MODEL PARENT_ID BEHAVIOR CONFIGURATION
    # =================================================================
    # Models that REQUIRE explicit parent_id for linear conversation history
    # (without parent_id they create "regeneration branches" instead of continuing dialogue)
    MODELS_REQUIRING_PARENT_ID = os.getenv(
        "QWEN_MODELS_REQUIRING_PARENT_ID",
        (
            "qwen3.7-max,"
            "qwen-latest-series-invite-beta-v16,"
            "qwen-latest-series-invite-beta-v24,"
            "qwen3.6-plus,"
            "qwen3.6-plus-preview,"
            "qwen3.6-max-preview,"
            "qwen3.5-plus,"
            "qwen3.5-flash,"
            "qwen3.5-omni-flash,"
            "qwen3.5-omni-plus,"
            "qwen3.5-max-2026-03-08,"
            "qwen3.5-max-preview,"
            "qwen3.5-27b,"
            "qwen3.5-35b-a3b,"
            "qwen3.5-122b-a10b,"
            "qwen3.5-397b-a17b,"
            "qwen3-omni-flash-2025-12-01,"
            "qwen3-vl-plus,"
            "qwen3-coder-plus,"
            "qwen3-max-2026-01-23,"
            "qwen3-max,"
            "qwen-plus-2025-07-28,"
            "qwen3-235b-a22b-2507,"
            "qwen3-vl-235b-a22b,"
            "qwen2.5-max"
        )
    ).split(",")
    # Models that work correctly WITH parent_id=None (auto-build history inside chat_id)
    MODELS_WORKING_WITHOUT_PARENT_ID = os.getenv(
        "QWEN_MODELS_WITHOUT_PARENT_ID",
        ""
    ).split(",")
    # Strip whitespace from model names (in case of spaces in env)
    MODELS_REQUIRING_PARENT_ID = [m.strip() for m in MODELS_REQUIRING_PARENT_ID if m.strip()]
    MODELS_WORKING_WITHOUT_PARENT_ID = [m.strip() for m in MODELS_WORKING_WITHOUT_PARENT_ID if m.strip()]
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

    # 🔥 POSTGRES AUTO MIGRATION
    # Select: true, false
    ENABLE_AUTO_MIGRATION: bool = os.getenv("ENABLE_AUTO_MIGRATION", "true").lower() in ("true", "1", "yes", "on")

    # 🔥 CHAT STATE BACKEND CONFIGURATION
    # Backend select: file, postgres
    _RAW_BACKEND: str = os.getenv("CHAT_STATE_BACKEND", "file").lower()
    try:
        CHAT_STATE_BACKEND: ChatStateBackendType = ChatStateBackendType(_RAW_BACKEND)
    except ValueError:
        raise ValueError(f"Wrong CHAT_STATE_BACKEND='{_RAW_BACKEND}'. Correct values: 'file', 'postgres'.")

    # 🔥 INDEPENDENT DATABASE FOR CHAT STATE
    # Dont have any relations with OPENWEBUI_DB_*
    CHAT_STATE_DB_HOST: str = os.getenv("CHAT_STATE_DB_HOST", "localhost")
    CHAT_STATE_DB_PORT: int = int(os.getenv("CHAT_STATE_DB_PORT", "5432"))
    CHAT_STATE_DB_NAME: str = os.getenv("CHAT_STATE_DB_NAME", "api3264_chat_state")
    CHAT_STATE_DB_USER: str = os.getenv("CHAT_STATE_DB_USER", "freeqwenapi")
    CHAT_STATE_DB_PASSWORD: str = os.getenv("CHAT_STATE_DB_PASSWORD", "freeqwenapi")
    CHAT_STATE_DB_TABLE: str = os.getenv("CHAT_STATE_DB_TABLE", "chat_mappings")
    CHAT_STATE_DB_POOL_MIN: int = int(os.getenv("CHAT_STATE_DB_POOL_MIN", "2"))
    CHAT_STATE_DB_POOL_MAX: int = int(os.getenv("CHAT_STATE_DB_POOL_MAX", "10"))

    # 🔥 TOKENS BACKEND CONFIGURATION
    # Backend select: file, postgres
    _TOKEN_RAW: str = os.getenv("TOKEN_STORAGE_BACKEND", "file").lower()
    try:
        TOKEN_STORAGE_BACKEND: TokenBackendType = TokenBackendType(_TOKEN_RAW)
    except ValueError:
        raise ValueError(f"Wrong TOKEN_STORAGE_BACKEND='{_TOKEN_RAW}'. Correct values: 'file', 'postgres'.")

    # 🔥 INDEPENDENT DATABASE FOR TOKENS
    # Dont have any relations with OPENWEBUI_DB_*
    TOKEN_DB_HOST: str = os.getenv("TOKEN_DB_HOST", "localhost")
    TOKEN_DB_PORT: int = int(os.getenv("TOKEN_DB_PORT", "5432"))
    TOKEN_DB_NAME: str = os.getenv("TOKEN_DB_NAME", "api3264_tokens")
    TOKEN_DB_USER: str = os.getenv("TOKEN_DB_USER", "freeqwenapi")
    TOKEN_DB_PASSWORD: str = os.getenv("TOKEN_DB_PASSWORD", "freeqwenapi")
    TOKEN_DB_TABLE: str = os.getenv("TOKEN_DB_TABLE", "tokens")
    TOKEN_DB_SSL_MODE: str = os.getenv("TOKEN_DB_SSL_MODE", "prefer")
    TOKEN_DB_POOL_MIN: int = int(os.getenv("TOKEN_DB_POOL_MIN", "2"))
    TOKEN_DB_POOL_MAX: int = int(os.getenv("TOKEN_DB_POOL_MAX", "10"))

    # 🔧 DELAYS CONFIGURATION
    # Delay before updating parent_id after stream ends (seconds)
    PARENT_ID_UPDATE_DELAY = float(os.getenv("PARENT_ID_UPDATE_DELAY", "1.5"))
    
    # Delay in processing tool_calls for synchronization with the API (seconds)
    TOOL_CALL_SYNC_DELAY = float(os.getenv("TOOL_CALL_SYNC_DELAY", "1.5"))

    # ===========================================================================
    # ERROR MESSAGES CONFIGURATION
    # ===========================================================================
    # Error messages and hints are externalized for flexibility and localization.
    # Supports three languages with TTS language tags: EN, RU, UA.
    # Override any message via environment variables in .env file.
    # ===========================================================================

    # 🔧 Flag to enable/disable error hints globally
    ERROR_HINTS_ENABLED: bool = os.getenv("ERROR_HINTS_ENABLED", "true").lower() in ("true", "1", "yes", "on")

    ERROR_MESSAGES = {
        "rate_limit_exceeded": {
            "message": os.getenv("ERR_RATE_LIMIT_MSG", " EN: Request limit exceeded. Service temporarily unavailable.\n RU: Лимит запросов исчерпан. Сервис временно недоступен.\n UA: Ліміт запитів вичерпано. Сервіс тимчасово недоступний.\n"),
            "hint": os.getenv("ERR_RATE_LIMIT_HINT", " EN: Try again in a few minutes.\n RU: Попробуйте повторить запрос через несколько минут.\n UA: Спробуйте повторити запит через кілька хвилин.\n")
        },
        "chat_locked": {
            "message": os.getenv("ERR_CHAT_LOCKED_MSG", " EN: Chat is busy processing a previous message.\n RU: Чат занят обработкой предыдущего сообщения.\n UA: Чат зайнятий обробкою попереднього повідомлення.\n"),
            "hint": os.getenv("ERR_CHAT_LOCKED_HINT", " EN: Wait for the current operation to complete and retry.\n RU: Подождите завершения текущей операции и повторите запрос.\n UA: Дочекайтеся завершення поточної операції та повторіть запит.\n")
        },
        "auth_failed": {
            "message": os.getenv("ERR_AUTH_FAILED_MSG", " EN: Authentication error on uplink server.\n RU: Ошибка аутентификации на вышестоящем сервере.\n UA: Помилка аутентифікації на вищому сервері.\n"),
            "hint": os.getenv("ERR_AUTH_FAILED_HINT", " EN: Administration has been notified and is taking action.\n RU: Администрация извещена и принимает меры.\n UA: Адміністрація повідомлена та вживає заходів.\n")
        },
        "model_overloaded": {
            "message": os.getenv("ERR_MODEL_OVERLOADED_MSG", " EN: Model is overloaded. Service is experiencing high load.\n RU: Модель перегружена. Сервис испытывает высокую нагрузку.\n UA: Модель перевантажена. Сервіс зазнає високого навантаження.\n"),
            "hint": os.getenv("ERR_MODEL_OVERLOADED_HINT", " EN: Retry later or select a different model.\n RU: Попробуйте повторить запрос позже или выберите другую модель.\n UA: Спробуйте повторити запит пізніше або оберіть іншу модель.\n")
        },
        "invalid_request": {
            "message": os.getenv("ERR_INVALID_REQUEST_MSG", " EN: Invalid request.\n RU: Некорректный запрос.\n UA: Некоректний запит.\n"),
            "hint": os.getenv("ERR_INVALID_REQUEST_HINT", " EN: Check request parameters (model, parent_id, message format).\n RU: Проверьте параметры запроса (модель, parent_id, формат сообщения).\n UA: Перевірте параметри запиту (модель, parent_id, формат повідомлення).\n")
        },
        "context_length_exceeded": {
            "message": os.getenv("ERR_CONTEXT_LENGTH_MSG", " EN: Context length exceeds the limit of {max_len} tokens.\n RU: Длина контекста превышает лимит в {max_len} токенов.\n UA: Довжина контексту перевищує ліміт у {max_len} токенів.\n"),
            "hint": os.getenv("ERR_CONTEXT_LENGTH_HINT", " EN: Shorten your message, clear history, or start a new chat.\n RU: Сократите сообщение, очистите историю или начните новый чат.\n UA: Скоротіть повідомлення, очистіть історію або почніть новий чат.\n")
        },
        "upstream_internal_error": {
            "message": os.getenv("ERR_INTERNAL_MSG", " EN: Internal error on uplink server side.\n RU: Внутренняя ошибка на стороне вышестоящего сервера.\n UA: Внутрішня помилка на стороні вищого сервера.\n"),
            "hint": os.getenv("ERR_INTERNAL_HINT", " EN: Administration has been notified and is taking action. Retry in a minute.\n RU: Администрация извещена и принимает меры. Попробуйте повторить запрос через минуту.\n UA: Адміністрація повідомлена та вживає заходів. Спробуйте повторити запит через хвилину.\n")
        },
        "network_timeout": {
            "message": os.getenv("ERR_TIMEOUT_MSG", " EN: Response timeout or connection error.\n RU: Превышено время ожидания ответа или ошибка соединения.\n UA: Перевищено час очікування відповіді або помилка з'єднання.\n"),
            "hint": os.getenv("ERR_TIMEOUT_HINT", " EN: Try again in a few minutes.\n RU: Попробуйте снова через несколько минут.\n UA: Спробуйте знову через кілька хвилин.\n")
        },
        "unknown_error": {
            "message": os.getenv("ERR_UNKNOWN_MSG", " EN: An error occurred while processing the request.\n RU: Произошла ошибка при обработке запроса.\n UA: Сталася помилка під час обробки запиту.\n"),
            "hint": os.getenv("ERR_UNKNOWN_HINT", " EN: Details are available for administrators in server logs.\n RU: Подробности доступны администрации в логах сервера.\n UA: Подробиці доступні адміністрації в логах сервера.\n")
        }
    }

    # 🔥 Settings validation for PostgreSQL mode
    if CHAT_STATE_BACKEND == ChatStateBackendType.POSTGRES:
        _required_pg_vars = ["CHAT_STATE_DB_HOST", "CHAT_STATE_DB_USER", "CHAT_STATE_DB_PASSWORD", "CHAT_STATE_DB_NAME"]
        _missing_pg_vars = [var for var in _required_pg_vars if not os.getenv(var)]
        if _missing_pg_vars:
            raise ValueError(f"CHAT_STATE_BACKEND=postgres require variables: {', '.join(_missing_pg_vars)}")

    # 🔥 Browser auth settings
    CHROME_USER_DATA: str = os.getenv("CHROME_USER_DATA", str(SCRIPT_DIR / "profile"))

    # Chromium executable path
    CHROMIUM_EXECUTABLE_PATH: str = os.getenv(
        "CHROMIUM_EXECUTABLE_PATH", 
        "/usr/lib/chromium/chromium"
    )

    # Chromium launch arguments (comma-separated in .env)
    _CHROMIUM_ARGS_RAW: str = os.getenv(
        "CHROMIUM_ARGS",
        "--no-sandbox,--no-first-run,--no-default-browser-check,--disable-features=TranslateUI"
    )
    CHROMIUM_ARGS: List[str] = [
        arg.strip() for arg in _CHROMIUM_ARGS_RAW.split(",") if arg.strip()
    ]

    # Arguments to ignore from Playwright defaults
    _CHROMIUM_IGNORE_ARGS_RAW: str = os.getenv(
        "CHROMIUM_IGNORE_DEFAULT_ARGS",
        "--enable-automation"
    )
    CHROMIUM_IGNORE_DEFAULT_ARGS: List[str] = [
        arg.strip() for arg in _CHROMIUM_IGNORE_ARGS_RAW.split(",") if arg.strip()
    ]

    # Viewport settings
    CHROME_VIEWPORT_WIDTH: int = int(os.getenv("CHROME_VIEWPORT_WIDTH", "1280"))
    CHROME_VIEWPORT_HEIGHT: int = int(os.getenv("CHROME_VIEWPORT_HEIGHT", "720"))

    # 🔥 HTTP client settings
    HTTP_TIMEOUT: float = float(os.getenv("HTTP_TIMEOUT", "900.0"))
    HTTP_FOLLOW_REDIRECTS: bool = os.getenv("HTTP_FOLLOW_REDIRECTS", "true").lower() in ("true", "1", "yes", "on")
    # 🔥 Default headers for Qwen API requests
    DEFAULT_HEADERS: Dict[str, str] = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, _/_",
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
    # 🔥 REMOVED: psycopg2 logging (now using asyncpg in db_async.py)
    return logging.getLogger("FreeQwenApi")
