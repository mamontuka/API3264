#!/usr/bin/env python3
#
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

"""
Простой тест Vision через api3264.
Берёт картинку из temp/, отправляет, получает ответ.
"""
import requests
import base64
import sys
from pathlib import Path

API_URL = "http://10.32.64.2:3264/api/chat/completions"
TEMP_DIR = Path("/root/ai/core/qwen/api3264/temp")

# Берём любую картинку из temp/
images = list(TEMP_DIR.glob("img_*.png")) + list(TEMP_DIR.glob("img_*.jpg"))
if not images:
    print(f"❌ Нет картинок в {TEMP_DIR}")
    sys.exit(1)

img_path = images[0]
print(f"🖼️  Картинка: {img_path.name}")

with open(img_path, "rb") as f:
    b64 = base64.b64encode(f.read()).decode("utf-8")

payload = {
    "model": "qwen3.5-max",
    "stream": False,
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Что на картинке?"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
            ]
        }
    ]
}

print("🚀 Отправляем...")
resp = requests.post(API_URL, json=payload, headers={"Content-Type": "application/json"}, timeout=200)

print(f"📥 Статус: {resp.status_code}")
if resp.status_code == 200:
    content = resp.json()["choices"][0]["message"]["content"]
    print(f"✅ Ответ:\n{content}")
else:
    print(f"❌ Ошибка: {resp.text}")
