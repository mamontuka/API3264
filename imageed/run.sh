#!/bin/bash
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

set -e

# ==========================================
# LOAD CONFIGURATION
# ==========================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"

if [[ -f "$ENV_FILE" ]]; then
    echo "📥 Loading configuration from $ENV_FILE..."
    set -a
    source "$ENV_FILE"
    set +a
else
    echo "❌ ERROR: .env file not found at $ENV_FILE"
    exit 1
fi

# ==========================================
# VALIDATION
# ==========================================
if [[ -z "$INSTANCE_NUM" || -z "$BASE_DIR" ]]; then
    echo "❌ ERROR: INSTANCE_NUM or BASE_DIR not defined in .env"
    exit 1
fi

# Check directory availability
if [[ ! -d "$BASE_DIR" ]]; then
    echo "❌ ERROR: Base directory does not exist: $BASE_DIR"
    exit 1
fi

# ==========================================
# ENVIRONMENT SETUP
# ==========================================
export CHROME_PATH="$CHROME_BIN"
export CHROME_USER_DATA="$PROFILE_DIR"
export LANG=ru_RU.UTF-8
export PUPPETEER_SKIP_DOWNLOAD=true

cd "$BASE_DIR"

echo "=============================================="
echo "🚀 Starting Image Editor for Instance $INSTANCE_NUM"
echo "=============================================="
echo "📁 Working Dir: $BASE_DIR"
echo "🖥️  DISPLAY: $DISPLAY_NUM"
echo "🌐 NetNS: $NETNS_NAME"
echo "🔗 Socat: $INSTANCE_IP:$CHROME_DEBUG_PORT_EXTERNAL -> 127.0.0.1:$CHROME_DEBUG_PORT_INTERNAL"
echo "🐍 Script: $VENV_PYTHON $SCRIPT_PATH"
echo "=============================================="

# ==========================================
# CLEANUP
# ==========================================
echo "🧹 Cleaning up previous processes..."
set +e

for port in $FLASK_PORT $CHROME_DEBUG_PORT_EXTERNAL $CHROME_DEBUG_PORT_INTERNAL; do
    /usr/bin/ip netns exec "$NETNS_NAME" fuser -k -9 ${port}/tcp 2>/dev/null || true
done

sleep 1
set -e

# ==========================================
# START COMPONENTS
# ==========================================

# 1. Socat (bounce outside browser debug port)
echo "🔌 Starting socat..."
/usr/bin/ip netns exec "$NETNS_NAME" \
    socat TCP-LISTEN:${CHROME_DEBUG_PORT_EXTERNAL},bind=${INSTANCE_IP},fork,reuseaddr \
          TCP:127.0.0.1:${CHROME_DEBUG_PORT_INTERNAL} &

# 2. Python Flask App
echo "🐍 Starting Flask application..."
/usr/bin/ip netns exec "$NETNS_NAME" \
    "$VENV_PYTHON" "$SCRIPT_PATH" &

# 3. Chrome Browser
echo "🌐 Starting Chrome..."
DISPLAY="$DISPLAY_NUM" \
/usr/bin/ip netns exec "$NETNS_NAME" \
    "$CHROME_BIN" \
    --user-data-dir="$PROFILE_DIR" \
    --no-first-run \
    --no-sandbox \
    --headless=new \
    --enable-unsafe-swiftshader \
    --no-default-browser-check \
    --disable-features=TranslateUI \
    --remote-debugging-port=${CHROME_DEBUG_PORT_INTERNAL} \
    2>/dev/null

AUTH_RC=$?

set -e
exit $AUTH_RC
