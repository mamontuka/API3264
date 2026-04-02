#!/bin/bash

#=================================================================
#-------------------------- Qwen API 3264 ------------------------
#=================================================================

    if [ ! -d "/root/ai/core/qwen/api3264/qwenapi-venv" ]; then
        echo "Создание виртуального окружения для Qwen API 3264..."
        /usr/local/bin/python3.11 -m venv /root/ai/core/qwen/api3264/qwenapi-venv
        source /root/ai/core/qwen/api3264/qwenapi-venv/bin/activate
        /root/ai/core/qwen/api3264/qwenapi-venv/bin/pip install --upgrade pip
        /root/ai/core/qwen/api3264/qwenapi-venv/bin/pip install -r /root/ai/core/qwen/template/requirements.txt
    fi

    echo "Venv done for Qwen API 3264 Server..."



