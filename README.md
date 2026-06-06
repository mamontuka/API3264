### Openwebui environment :

    ####################################
    # 🔐 SECURITY & AUTH
    ####################################

    # SECRETS: Ensure this file is not accessible from outside (chmod 600)
    WEBUI_SECRET_KEY="xxxxxxxxxxxxxxxxxxxxxx"

    # CORS: Only own domain allowed
    CORS_ALLOW_ORIGIN="https://ai.xxxxxx.com"
    #CORS_ALLOW_CUSTOM_SCHEME=""

    # Trust headers (if behind reverse proxy)
    ENABLE_FORWARD_USER_INFO_HEADERS="True"
    CUSTOM_HEADERS="X-Chat-ID:${CHAT_ID}"

    DOCKER="False"
    USE_CUDA_DOCKER="False"
    USE_CUDA="True"

    ####################################
    # 🌐 NETWORK & UI
    ####################################

    WEBUI_NAME="xxxxxx AI Cloud"

    # Behind Nginx
    WEBUI_HOST="127.0.0.1"
    WEBUI_PORT="8181"

    USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"

    # Hide user counter
    ENABLE_PUBLIC_ACTIVE_USERS_COUNT="False"

    ####################################
    # 🐘 DATABASE: PostgreSQL + PGVector
    ####################################

    # Unified connection string for main DB and vectors
    DATABASE_URL="postgresql://openwebui:openwebui@localhost:5432/openwebui"

    # Enable PGVector for RAG scalability
    VECTOR_DB="pgvector"
    PGVECTOR_DB_URL="${DATABASE_URL}"

    # Connection pool (UVICORN_WORKERS=25)
    # max_connections in Postgres >= 50000
    DATABASE_POOL_SIZE="30"
    DATABASE_POOL_MAX_OVERFLOW="10"
    DATABASE_POOL_TIMEOUT="30"
    DATABASE_POOL_RECYCLE="1800"

    # Pool settings for vector operations
    PGVECTOR_POOL_SIZE="20"
    PGVECTOR_INDEX_METHOD="hnsw"
    PGVECTOR_HNSW_M="16"

    # Write optimization
    ENABLE_REALTIME_CHAT_SAVE="True"
    ENABLE_QUERIES_CACHE="True"
    #DATABASE_USER_ACTIVE_STATUS_UPDATE_INTERVAL="60.0"

    ####################################
    # 🧠 REDIS: Scaling & WebSockets
    ####################################

    REDIS_URL="redis://localhost:6379/0"
    REDIS_KEY_PREFIX="ai-core-webui"
    WEBSOCKET_MANAGER="redis"
    ENABLE_WEBSOCKET_SUPPORT="True"
    WEBSOCKET_REDIS_LOCK_TIMEOUT="600"

    ####################################
    # ⚙️ APP: Workers & Performance
    ####################################

    # Workers: adjust based on CPU cores +1
    UVICORN_WORKERS="25"

    # Logging: ERROR for production (less noise, higher speed)
    GLOBAL_LOG_LEVEL="ERROR"

    # Offline mode (disables update checks and external hubs)
    OFFLINE_MODE="True"

    ####################################
    # 📡 CLIENT TIMEOUTS & STREAMING
    ####################################

    # Unified timeouts
    AIOHTTP_CLIENT_TIMEOUT="600"
    AIOHTTP_CLIENT_TOTAL_TIMEOUT="900"
    AIOHTTP_CLIENT_CONN_TIMEOUT="600"
    AIOHTTP_CLIENT_SOCK_TIMEOUT="600"
    AIOHTTP_READ_BUFSIZE="10485760"

    ENABLE_STREAMING="true"
    STREAMING_TIMEOUT="900"

    # Streaming buffer: 100MB (for large responses)
    CHAT_STREAM_RESPONSE_CHUNK_MAX_BUFFER_SIZE="104857600"

    # Persistent config is usually disabled by default
    # ENABLE_PERSISTENT_CONFIG="True"
    ENABLE_EASTER_EGGS="False"

### env.example - copy to file named .env and adjust settings

### in Openwebui Admin Panel -> Functions :

    """
    title: Adapter openai-qwen
    description: Sends only the last user message to reduce context sent into qwen
    author: Oleh Mamont
    version: 1.0.0
    type: filter
    """

    from typing import Optional, List
    from pydantic import BaseModel


    class Valves(BaseModel):
        pipelines: List[str] = ["*"]
        priority: int = 0


    class Filter:
        def __init__(self):
            self.type = "filter"
            self.name = "Last Message Only"
            self.valves = Valves()

        async def inlet(self, body: dict, user: Optional[dict] = None) -> dict:
            messages = body.get("messages", [])
            if len(messages) > 1:
                system_msgs = [m for m in messages if m.get("role") == "system"]
                last_user_msg = next(
                    (m for m in reversed(messages) if m.get("role") == "user"), None
                )
                if last_user_msg:
                    body["messages"] = system_msgs + [last_user_msg]
            return body

### *and enable filter for Qwen models or globaly if not have any else models*
