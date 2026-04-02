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
# Тестовый скрипт для проверки работы chatId
# =================================================================

API_URL="http://10.32.64.2:3264/api/chat/completions"
MODEL="qwen3-coder-plus"

# Chat ID из лога (это ID который генерирует ваш фронтенд)
EXISTING_CHAT_ID="6a9d5d34-a67f-4f0b-b3da-5eb31ad401b3"

# Новый Chat ID для сравнения
NEW_CHAT_ID=$(cat /proc/sys/kernel/random/uuid)

echo "============================================================"
echo "           ТЕСТ СОХРАНЕНИЯ CHAT ID"
echo "============================================================"
echo ""
echo "API Endpoint: $API_URL"
echo "Model: $MODEL"
echo ""
echo "Существующий chatId (из лога): $EXISTING_CHAT_ID"
echo "Новый chatId (для сравнения): $NEW_CHAT_ID"
echo ""

# Функция для отправки запроса
send_request() {
    local chat_id=$1
    local message=$2
    local test_name=$3
    
    echo "------------------------------------------------------------"
    echo "ТЕСТ: $test_name"
    echo "------------------------------------------------------------"
    echo "chatId: $chat_id"
    echo "Сообщение: $message"
    echo ""
    echo "Отправка запроса..."
    echo ""
    
    # Отправляем запрос и сохраняем ответ
    response=$(curl -s -X POST "$API_URL" \
        -H "Content-Type: application/json" \
        -d "{
            \"model\": \"$MODEL\",
            \"messages\": [
                {\"role\": \"user\", \"content\": \"$message\"}
            ],
            \"chatId\": \"$chat_id\",
            \"stream\": false
        }" \
        -w "\n%{http_code}" \
        --max-time 30)
    
    # Разделяем тело ответа и HTTP код
    http_code=$(echo "$response" | tail -n1)
    body=$(echo "$response" | sed '$d')
    
    echo "HTTP Status: $http_code"
    echo ""
    
    if [ "$http_code" = "200" ]; then
        echo "✅ Успешный ответ!"
        echo ""
        echo "Тело ответа (первые 500 символов):"
        echo "$body" | head -c 500
        echo ""
        echo ""
        
        # Пытаемся извлечь chatId из ответа (если есть)
        response_chat_id=$(echo "$body" | grep -o '"chatId":"[^"]*"' | head -1 | cut -d'"' -f4)
        if [ ! -z "$response_chat_id" ]; then
            echo "chatId из ответа: $response_chat_id"
        fi
        
        # Пытаемся извлечь parentId из ответа
        response_parent_id=$(echo "$body" | grep -o '"parentId":"[^"]*"' | head -1 | cut -d'"' -f4)
        if [ ! -z "$response_parent_id" ]; then
            echo "parentId из ответа: $response_parent_id"
        fi
        
    else
        echo "❌ Ошибка!"
        echo ""
        echo "Тело ответа:"
        echo "$body" | head -c 1000
        echo ""
    fi
    
    echo ""
    echo ""
}

# =================================================================
# ТЕСТ 1: Отправка с существующим chatId
# =================================================================
send_request "$EXISTING_CHAT_ID" "Это продолжение диалога. Если chatId работает правильно, это сообщение должно добавиться в существующий чат Qwen." "Существующий chatId"

# Пауза между запросами
sleep 2

# =================================================================
# ТЕСТ 2: Отправка с новым chatId (для сравнения)
# =================================================================
send_request "$NEW_CHAT_ID" "Это новый диалог. Должен создаться новый чат на стороне Qwen." "Новый chatId"

# =================================================================
# ИНСТРУКЦИЯ
# =================================================================
echo "============================================================"
echo "                    ПРОВЕРКА"
echo "============================================================"
echo ""
echo "Теперь посмотрите в логи вашего сервера:"
echo ""
echo "  tail -f /root/ai/log/ai-qwenapi-3264.log"
echo ""
echo "Ищите строки:"
echo "  - \"Найден существующий чат: $EXISTING_CHAT_ID\" (ОЖИДАЕМО для ТЕСТ 1)"
echo "  - \"Создание нового чата Qwen для $EXISTING_CHAT_ID\" (НЕ ОЖИДАЕМО для ТЕСТ 1)"
echo "  - \"Создание нового чата Qwen для $NEW_CHAT_ID\" (ОЖИДАЕМО для ТЕСТ 2)"
echo ""
echo "Если для ТЕСТ 1 вы видите \"Создание нового чата\" — значит,"
echo "ваш клиент (фронтенд) каждый раз генерирует новый chatId,"
echo "и проблема именно в нём, а не в прокси."
echo ""
echo "============================================================"
