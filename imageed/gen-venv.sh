#!/bin/bash

#=================================================================
#------------------------ Image Editor 7264 ----------------------
#=================================================================

    if [ ! -d "/root/ai/core/qwen/api3264/imageed/imageed-venv" ]; then
        echo "Creating virtual environment for Image Editor 7264..."
        /usr/local/bin/python3.11 -m venv /root/ai/core/qwen/api3264/imageed/imageed-venv
        source /root/ai/core/qwen/api3264/imageed/imageed-venv/bin/activate
        /root/ai/core/qwen/api3264/imageed/imageed-venv/bin/pip install --upgrade pip
        /root/ai/core/qwen/api3264/imageed/imageed-venv/bin/pip install -r /root/ai/core/qwen/api3264/imageed/requirements.txt
    fi

    echo "Venv done for Image Editor 7264..."



