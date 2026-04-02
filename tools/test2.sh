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
# ТЕСТ СТРИМИНГА ОТВЕТА ОТ QWEN
# =================================================================

API_URL="http://10.32.64.2:3264/api/chat/completions"
MODEL="qwen3-coder-plus"
TEST_CHAT_ID="stream-test-$(date +%s)"

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}============================================================${NC}"
echo -e "${BLUE}           ТЕСТ СТРИМИНГА ОТВЕТА ОТ МОДЕЛИ${NC}"
echo -e "${BLUE}============================================================${NC}"
echo ""
echo "API Endpoint: $API_URL"
echo "Model: $MODEL"
echo "Test chatId: $TEST_CHAT_ID"
echo ""

# =================================================================
# ФУНКЦИЯ: Тест нестримингового ответа (базовый)
# =================================================================
test_non_stream() {
    echo -e "${YELLOW}────────────────────────────────────────────────────${NC}"
    echo -e "${YELLOW}ТЕСТ 1: Нестриминговый ответ (stream: false)${NC}"
    echo -e "${YELLOW}────────────────────────────────────────────────────${NC}"
    echo ""
    
    local start_time=$(date +%s%N)
    
    response=$(curl -s -w "\n%{http_code}\n%{time_total}" -X POST "$API_URL" \
        -H "Content-Type: application/json" \
        -d "{
            \"model\": \"$MODEL\",
            \"messages\": [{\"role\": \"user\", \"content\": \"Ответь одним словом: ПРИВЕТ\"}],
            \"chatId\": \"$TEST_CHAT_ID-nonstream\",
            \"stream\": false
        }" \
        --max-time 30)
    
    local end_time=$(date +%s%N)
    local duration=$(( (end_time - start_time) / 1000000 ))
    
    local http_code=$(echo "$response" | tail -n2 | head -n1)
    local total_time=$(echo "$response" | tail -n1)
    local body=$(echo "$response" | sed '$d' | sed '$d')
    
    echo -e "⏱  Время выполнения: ${GREEN}${duration}мс${NC} (curl: ${total_time}с)"
    echo -e "📡 HTTP Status: $([ "$http_code" = "200" ] && echo -e "${GREEN}$http_code✅${NC}" || echo -e "${RED}$http_code❌${NC}")"
    echo ""
    
    if [ "$http_code" = "200" ]; then
        # Извлекаем content из ответа
        content=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('choices',[{}])[0].get('message',{}).get('content',''))" 2>/dev/null)
        echo -e "📝 Ответ модели:${NC}"
        echo -e "   ${GREEN}$content${NC}"
        echo ""
        
        # Проверяем заголовки ответа
        echo -e "🔍 Проверка заголовков ответа:"
        curl -s -I -X POST "$API_URL" \
            -H "Content-Type: application/json" \
            -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"test\"}],\"chatId\":\"$TEST_CHAT_ID-hdr\",\"stream\":false}" \
            --max-time 10 2>/dev/null | grep -iE "(content-type|cache-control|transfer-encoding|connection)" | sed 's/^/   /'
    else
        echo -e "${RED}❌ Ошибка:${NC}"
        echo "$body" | head -c 500
    fi
    echo ""
}

# =================================================================
# ФУНКЦИЯ: Тест стримингового ответа с детальным выводом
# =================================================================
test_stream() {
    echo -e "${YELLOW}────────────────────────────────────────────────────${NC}"
    echo -e "${YELLOW}ТЕСТ 2: Стриминговый ответ (stream: true)${NC}"
    echo -e "${YELLOW}────────────────────────────────────────────────────${NC}"
    echo ""
    
    local output_file="/tmp/stream_test_$(date +%s).log"
    local chunks_received=0
    local first_chunk_time=""
    local last_chunk_time=""
    
    echo -e "📡 Отправка запроса с stream: true..."
    echo -e "📁 Лог сырых данных: ${BLUE}$output_file${NC}"
    echo ""
    echo -e "${GREEN}Получаем чанки (каждый '.' = один чанк):${NC}"
    echo -n "   "
    
    # Запускаем curl в фоне, читаем построчно
    curl -s -N -X POST "$API_URL" \
        -H "Content-Type: application/json" \
        -H "Accept: text/event-stream" \
        -d "{
            \"model\": \"$MODEL\",
            \"messages\": [{\"role\": \"user\", \"content\": \"Напиши 5 слов через запятую, каждое с новой строки в ответе\"}],
            \"chatId\": \"$TEST_CHAT_ID-stream\",
            \"stream\": true
        }" \
        --max-time 45 \
        -w "\n%{http_code}\n%{time_total}" \
        2>/dev/null | tee "$output_file" | while IFS= read -r line; do
        # Обрабатываем SSE-формат
        if [[ "$line" =~ ^data:\ * ]]; then
            data="${line#data: }"
            data="${data#"${data%%[![:space:]]*}"}"  # trim left
            
            if [ "$data" = "[DONE]" ]; then
                echo -e "\n   ${BLUE}[DONE]${NC}"
                break
            fi
            
            if [ -n "$data" ] && [ "$data" != "" ]; then
                # Печатаем индикатор получения чанка
                echo -n "."
                
                # Засекаем время первого и последнего чанка
                if [ -z "$first_chunk_time" ]; then
                    first_chunk_time=$(date +%s%N)
                fi
                last_chunk_time=$(date +%s%N)
                
                chunks_received=$((chunks_received + 1))
                
                # Извлекаем и печатаем контент чанка (если есть)
                content=$(echo "$data" | python3 -c "import sys,json; d=json.load(sys.stdin); c=d.get('choices',[{}])[0].get('delta',{}).get('content',''); print(c,end='')" 2>/dev/null)
                if [ -n "$content" ]; then
                    echo -n "${content}"
                fi
            fi
        fi
    done
    
    echo ""
    echo ""
    
    # Анализируем результаты
    local http_code=$(tail -n2 "$output_file" | head -n1)
    local total_time=$(tail -n1 "$output_file")
    
    echo -e "📊 Статистика стриминга:"
    echo -e "   📦 Чанков получено: ${GREEN}$chunks_received${NC}"
    
    if [ -n "$first_chunk_time" ] && [ -n "$last_chunk_time" ]; then
        local stream_duration=$(( (last_chunk_time - first_chunk_time) / 1000000 ))
        echo -e "   ⏱  Длительность стрима: ${GREEN}${stream_duration}мс${NC}"
        if [ "$chunks_received" -gt 0 ]; then
            local avg_interval=$(( stream_duration / chunks_received ))
            echo -e "   🔄 Средний интервал между чанками: ${GREEN}${avg_interval}мс${NC}"
        fi
    fi
    
    echo -e "   📡 HTTP Status: $([ "$http_code" = "200" ] && echo -e "${GREEN}$http_code✅${NC}" || echo -e "${RED}$http_code❌${NC}")"
    echo -e "   🌐 Общее время: ${total_time}с"
    echo ""
    
    # Проверка заголовков для стриминга
    echo -e "🔍 Проверка заголовков (стриминг):"
    curl -s -I -X POST "$API_URL" \
        -H "Content-Type: application/json" \
        -H "Accept: text/event-stream" \
        -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"test\"}],\"chatId\":\"$TEST_CHAT_ID-hdr2\",\"stream\":true}" \
        --max-time 10 2>/dev/null | grep -iE "(content-type|cache-control|transfer-encoding|connection|x-accel)" | sed 's/^/   /'
    echo ""
    
    # Проверка формата ответа
    echo -e "🔍 Анализ формата ответа:"
    local content_type=$(curl -s -I -X POST "$API_URL" \
        -H "Content-Type: application/json" \
        -H "Accept: text/event-stream" \
        -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"test\"}],\"chatId\":\"$TEST_CHAT_ID-ct\",\"stream\":true}" \
        --max-time 10 2>/dev/null | grep -i "^content-type:" | head -1)
    
    if echo "$content_type" | grep -qi "text/event-stream"; then
        echo -e "   ✅ Content-Type корректный: ${GREEN}$content_type${NC}"
    elif echo "$content_type" | grep -qi "application/json"; then
        echo -e "   ⚠️  Content-Type JSON вместо SSE: ${YELLOW}$content_type${NC}"
        echo -e "      Возможно, модель ответила сразу целиком (короткий ответ)"
    else
        echo -e "   ❌ Неожиданный Content-Type: ${RED}$content_type${NC}"
    fi
    echo ""
    
    # Показываем первые и последние чанки для отладки
    echo -e "🔍 Примеры чанков (первые 3):"
    grep "^data:" "$output_file" | head -3 | while read -r line; do
        data="${line#data: }"
        if [ "$data" != "[DONE]" ] && [ -n "$data" ]; then
            echo "   $data" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'   delta: {d.get(\"choices\":[{}])[0].get(\"delta\",{})}')" 2>/dev/null || echo "   (parse error)"
        fi
    done
    echo ""
    
    echo -e "🔍 Примеры чанков (последние 3):"
    grep "^data:" "$output_file" | tail -3 | while read -r line; do
        data="${line#data: }"
        if [ "$data" != "[DONE]" ] && [ -n "$data" ]; then
            echo "   $data" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'   delta: {d.get(\"choices\":[{}])[0].get(\"delta\",{})}')" 2>/dev/null || echo "   (parse error)"
        fi
    done
    echo ""
    
    # Очистка
    rm -f "$output_file"
}

# =================================================================
# ФУНКЦИЯ: Тест "быстрого" ответа (короткий промпт)
# =================================================================
test_stream_short() {
    echo -e "${YELLOW}────────────────────────────────────────────────────${NC}"
    echo -e "${YELLOW}ТЕСТ 3: Стриминг короткого ответа (1-2 слова)${NC}"
    echo -e "${YELLOW}────────────────────────────────────────────────────${NC}"
    echo ""
    
    echo -e "📡 Отправка запроса с очень коротким ожидаемым ответом..."
    echo ""
    
    curl -s -N -X POST "$API_URL" \
        -H "Content-Type: application/json" \
        -H "Accept: text/event-stream" \
        -d "{
            \"model\": \"$MODEL\",
            \"messages\": [{\"role\": \"user\", \"content\": \"Скажи только: ОК\"}],
            \"chatId\": \"$TEST_CHAT_ID-short\",
            \"stream\": true
        }" \
        --max-time 20 \
        2>/dev/null | while IFS= read -r line; do
        if [[ "$line" =~ ^data:\ * ]]; then
            data="${line#data: }"
            data="${data#"${data%%[![:space:]]*}"}"
            if [ "$data" = "[DONE]" ]; then
                echo -e "\n${BLUE}[DONE]${NC}"
                break
            fi
            if [ -n "$data" ]; then
                content=$(echo "$data" | python3 -c "import sys,json; d=json.load(sys.stdin); c=d.get('choices',[{}])[0].get('delta',{}).get('content',''); print(c,end='')" 2>/dev/null)
                if [ -n "$content" ]; then
                    echo -n "${content}"
                fi
            fi
        fi
    done
    echo ""
    echo ""
}

# =================================================================
# ФУНКЦИЯ: Проверка заголовков прокси
# =================================================================
check_proxy_headers() {
    echo -e "${YELLOW}────────────────────────────────────────────────────${NC}"
    echo -e "${YELLOW}ТЕСТ 4: Проверка заголовков прокси для стриминга${NC}"
    echo -e "${YELLOW}────────────────────────────────────────────────────${NC}"
    echo ""
    
    echo -e "🔍 Заголовки ответа прокси при stream:true:"
    curl -s -i -X POST "$API_URL" \
        -H "Content-Type: application/json" \
        -H "Accept: text/event-stream" \
        -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"test\"}],\"chatId\":\"$TEST_CHAT_ID-hdr3\",\"stream\":true}" \
        --max-time 15 2>/dev/null | head -20
    echo ""
    
    echo -e "🔍 Заголовки ответа прокси при stream:false:"
    curl -s -i -X POST "$API_URL" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"test\"}],\"chatId\":\"$TEST_CHAT_ID-hdr4\",\"stream\":false}" \
        --max-time 15 2>/dev/null | head -20
    echo ""
}

# =================================================================
# ОСНОВНОЙ ЗАПУСК
# =================================================================
echo "Нажмите Enter для начала тестов (или Ctrl+C для выхода)..."
read -r

test_non_stream
sleep 2

test_stream
sleep 2

test_stream_short
sleep 2

check_proxy_headers

# =================================================================
# ИТОГИ
# =================================================================
echo -e "${BLUE}============================================================${NC}"
echo -e "${BLUE}                    ИТОГИ ТЕСТА${NC}"
echo -e "${BLUE}============================================================${NC}"
echo ""
echo -e "${GREEN}✅ Что проверить в логах прокси:${NC}"
echo "   1. При stream:true: заголовок Content-Type должен быть 'text/event-stream'"
echo "   2. Должны быть заголовки:"
echo "      - Cache-Control: no-cache"
echo "      - X-Accel-Buffering: no"
echo "      - Connection: keep-alive"
echo "   3. В логах должны быть строки '📤 Отправка запроса в чат...'"
echo "   4. Для SSE: curl должен получать строки вида 'data: {...}'"
echo ""
echo -e "${YELLOW}🐛 Частые проблемы и решения:${NC}"
echo "   • Ответ приходит целиком, а не чанками → проверить, что Qwen API возвращает SSE"
echo "   • Чанки есть, но не отображаются → проверить парсинг 'data:' в _stream_openai_response"
echo "   • Таймауты → увеличить timeout в http_client и curl"
echo "   • Буферизация nginx/proxy → добавить X-Accel-Buffering: no"
echo ""
echo -e "${BLUE}📋 Для отладки пришлите:${NC}"
echo "   1. Вывод этого скрипта"
echo "   2. Фрагмент лога прокси за время выполнения ТЕСТ 2"
echo "   3. Результат: curl -v ... (если проблема в сети)"
echo ""
