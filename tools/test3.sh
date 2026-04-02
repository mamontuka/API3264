#!/bin/bash
# Copyright (C) 2026 Oleh Mamont
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

# =================================================================
# inspect_openwebui_db.sh
# Анализ структуры базы данных OpenWebUI через PostgreSQL socket
# Запускать от имени пользователя с доступом к postgres socket
# =================================================================

set -e

# =================================================================
# CONFIGURATION (адаптируйте под вашу установку)
# =================================================================
# Путь к сокету PostgreSQL (обычно один из этих):
PG_SOCKET_DIR="${PG_SOCKET_DIR:-/var/run/postgresql}"
# PG_SOCKET_DIR="/tmp"  # Альтернативный путь

# Имя базы данных OpenWebUI
DB_NAME="${DB_NAME:-openwebui}"

# Суперпользователь PostgreSQL (обычно 'postgres')
PG_USER="${PG_USER:-postgres}"

# Выводить ли данные (может быть много)
SHOW_SAMPLE_DATA="${SHOW_SAMPLE_DATA:-true}"

# =================================================================
# COLORS & FORMATTING
# =================================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warning() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# =================================================================
# MAIN
# =================================================================

echo "============================================================"
echo "🔍 OpenWebUI PostgreSQL Database Inspector"
echo "============================================================"
echo "Socket: $PG_SOCKET_DIR"
echo "Database: $DB_NAME"
echo "User: $PG_USER"
echo "Show sample data: $SHOW_SAMPLE_DATA"
echo "============================================================"
echo ""

# Проверка доступа к сокету
if [ ! -S "$PG_SOCKET_DIR/.s.PGSQL.5432" ]; then
    warning "Socket $PG_SOCKET_DIR/.s.PGSQL.5432 not found, trying /tmp..."
    PG_SOCKET_DIR="/tmp"
    if [ ! -S "$PG_SOCKET_DIR/.s.PGSQL.5432" ]; then
        error "PostgreSQL socket not found in $PG_SOCKET_DIR or /tmp"
        echo ""
        echo "💡 Попробуйте найти сокет:"
        echo "   find / -name '.s.PGSQL.5432' 2>/dev/null"
        echo "   sudo -u postgres psql -c 'SHOW unix_socket_directories;'"
        exit 1
    fi
fi
success "Found PostgreSQL socket: $PG_SOCKET_DIR/.s.PGSQL.5432"

# Проверка доступа к базе
if ! sudo -u "$PG_USER" psql -h "$PG_SOCKET_DIR" -lqt | cut -d \| -f 1 | grep -qw "$DB_NAME"; then
    warning "Database '$DB_NAME' not found. Available databases:"
    sudo -u "$PG_USER" psql -h "$PG_SOCKET_DIR" -lqt | cut -d \| -f 1 | grep -v '^$' | sed 's/^[[:space:]]*//'
    echo ""
    read -p "Continue anyway? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi
success "Database '$DB_NAME' exists"

echo ""
echo "============================================================"
echo "📋 1. ВСЕ ТАБЛИЦЫ В БАЗЕ"
echo "============================================================"
sudo -u "$PG_USER" psql -h "$PG_SOCKET_DIR" -d "$DB_NAME" -c "\dt"

echo ""
echo "============================================================"
echo "🔍 2. ТАБЛИЦЫ, СВЯЗАННЫЕ С ЧАТАМИ/КОНВЕРСАЦИЯМИ"
echo "============================================================"
# Ищем таблицы с ключевыми словами
for pattern in "conversation" "chat" "message" "thread" "session" "history"; do
    echo -e "\n${YELLOW}🔎 Поиск таблиц с '$pattern':${NC}"
    sudo -u "$PG_USER" psql -h "$PG_SOCKET_DIR" -d "$DB_NAME" -c "\dt *${pattern}*" 2>/dev/null || true
done

echo ""
echo "============================================================"
echo "📐 3. СТРУКТУРА КЛЮЧЕВЫХ ТАБЛИЦ"
echo "============================================================"

# Функция для показа структуры таблицы
show_table_structure() {
    local table="$1"
    echo -e "\n${GREEN}📊 Таблица: $table${NC}"
    echo "------------------------------------------------------------"
    sudo -u "$PG_USER" psql -h "$PG_SOCKET_DIR" -d "$DB_NAME" -c "\d+ $table" 2>/dev/null || warning "Table '$table' not found"
}

# Проверяем распространённые имена таблиц
for table in "conversation" "chat" "conversations" "chats" "message" "messages" "user_conversation"; do
    # Проверяем, существует ли таблица
    if sudo -u "$PG_USER" psql -h "$PG_SOCKET_DIR" -d "$DB_NAME" -tAc "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='$table'" 2>/dev/null | grep -q 1; then
        show_table_structure "$table"
    fi
done

echo ""
echo "============================================================"
echo "🔑 4. КОЛОНКИ С ID ПОЛЬЗОВАТЕЛЕЙ И ЧАТОВ"
echo "============================================================"
# Ищем колонки, которые могут содержать user_id или conversation_id
echo -e "${YELLOW}🔎 Поиск колонок с 'user' и 'id':${NC}"
sudo -u "$PG_USER" psql -h "$PG_SOCKET_DIR" -d "$DB_NAME" -c "
SELECT 
    table_name, 
    column_name, 
    data_type,
    is_nullable
FROM information_schema.columns 
WHERE table_schema = 'public' 
  AND (column_name ILIKE '%user%id%' 
       OR column_name ILIKE '%conversation%id%' 
       OR column_name ILIKE '%chat%id%'
       OR column_name = 'user_id'
       OR column_name = 'id')
ORDER BY table_name, ordinal_position;
"

echo ""
echo "============================================================"
echo "👥 5. ПОЛЬЗОВАТЕЛИ (для проверки user_id)"
echo "============================================================"
# Показываем таблицу пользователей, если есть
for user_table in "user" "users" "account" "accounts"; do
    if sudo -u "$PG_USER" psql -h "$PG_SOCKET_DIR" -d "$DB_NAME" -tAc "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='$user_table'" 2>/dev/null | grep -q 1; then
        echo -e "\n${GREEN}📊 Таблица пользователей: $user_table${NC}"
        sudo -u "$PG_USER" psql -h "$PG_SOCKET_DIR" -d "$DB_NAME" -c "SELECT column_name, data_type FROM information_schema.columns WHERE table_name='$user_table' AND table_schema='public';"
        if [ "$SHOW_SAMPLE_DATA" = "true" ]; then
            echo "Пример данных (первые 3 строки):"
            sudo -u "$PG_USER" psql -h "$PG_SOCKET_DIR" -d "$DB_NAME" -c "SELECT * FROM $user_table LIMIT 3;" 2>/dev/null || true
        fi
        break
    fi
done

echo ""
echo "============================================================"
echo "💬 6. ПРИМЕРЫ ДАННЫХ ИЗ ТАБЛИЦ С ЧАТАМИ"
echo "============================================================"
if [ "$SHOW_SAMPLE_DATA" = "true" ]; then
    for table in "conversation" "chat" "conversations" "chats"; do
        if sudo -u "$PG_USER" psql -h "$PG_SOCKET_DIR" -d "$DB_NAME" -tAc "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='$table'" 2>/dev/null | grep -q 1; then
            echo -e "\n${GREEN}📊 Примеры из таблицы: $table${NC}"
            echo "Структура колонок:"
            sudo -u "$PG_USER" psql -h "$PG_SOCKET_DIR" -d "$DB_NAME" -c "SELECT column_name FROM information_schema.columns WHERE table_name='$table' AND table_schema='public' ORDER BY ordinal_position;"
            echo "Пример записи:"
            sudo -u "$PG_USER" psql -h "$PG_SOCKET_DIR" -d "$DB_NAME" -c "SELECT * FROM $table ORDER BY updated_at DESC LIMIT 1;" 2>/dev/null || true
        fi
    done
fi

echo ""
echo "============================================================"
echo "🎯 7. РЕКОМЕНДАЦИИ ДЛЯ НАШЕГО ПРОКСИ"
echo "============================================================"
echo "На основе найденных таблиц, обновите в config.py:"
echo ""
echo "# В функции _get_openwebui_chat_id_from_db():"
echo "1. Используйте правильное имя таблицы (из пункта 2 выше)"
echo "2. Используйте правильные имена колонок (из пунктов 3-4)"
echo "3. Пример запроса:"
echo "   SELECT id FROM <table_name>"
echo "   WHERE user_id = %s"
echo "   ORDER BY updated_at DESC LIMIT 1"
echo ""
echo "Если таблица 'conversation' не найдена, возможно в вашей"
echo "версии OpenWebUI используется другое имя, например:"
echo "  - chat"
echo "  - conversations" 
echo "  - user_chat"
echo "  - session"
echo ""

echo "============================================================"
echo "✅ Анализ завершён!"
echo "============================================================"
