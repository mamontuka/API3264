### Openwebui environment :

    # AI System Environment Configuration
    #ENABLE_PERSISTENT_CONFIG=True
    USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    GLOBAL_LOG_LEVEL="ERROR"
    CORS_ALLOW_ORIGIN="https://ai.xxxxxx.com"
    #CORS_ALLOW_ORIGIN="*"
    CORS_ALLOW_CUSTOM_SCHEME="https://ai.xxxxxx.com"
    WEBUI_PORT="8181"
    WEBUI_HOST="127.0.0.1"
    WEBUI_NAME="xxxxxx AI Cloud"
    WEBUI_SECRET_KEY="xxxxxxxxxxxxxxx"
    OFFLINE_MODE="True"
    DATABASE_URL="postgresql://openwebui:openwebui@localhost:5432/openwebui"
    REDIS_URL="redis://localhost:6379/0"
    WEBSOCKET_MANAGER="redis"
    ENABLE_WEBSOCKET_SUPPORT="True"
    ENABLE_FORWARD_USER_INFO_HEADERS="True"
    CUSTOM_HEADERS="X-Chat-ID:${CHAT_ID}"
    CHAT_STREAM_RESPONSE_CHUNK_MAX_BUFFER_SIZE="104857600"
    AIOHTTP_CLIENT_TIMEOUT="600"
    AIOHTTP_CLIENT_TIMEOUT=600
    AIOHTTP_CLIENT_TOTAL_TIMEOUT=900
    AIOHTTP_CLIENT_CONN_TIMEOUT=600
    AIOHTTP_CLIENT_SOCK_TIMEOUT=600
    ENABLE_STREAMING=true
    STREAMING_TIMEOUT=900

### env.example - copy to file named .env and adjust settings
