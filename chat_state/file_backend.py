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
File-based backend for Chat State storage.
Implements persistent storage using JSON file with atomic writes.
Supports all operations required by ChatStateBackend interface.
"""
import json
import logging
import asyncio
from pathlib import Path
from typing import Optional

from .base import ChatStateBackend, ChatStateData

logger = logging.getLogger("FreeQwenApi")


class FileBackend(ChatStateBackend):
    def __init__(self, file_path: Path):
        self.file_path = file_path
        self._lock = asyncio.Lock()
        self._data: dict[str, dict] = {}

    def _get_file_path(self, openweb_id: str, model: Optional[str] = None) -> Path:
        """Generates a file path based on the model."""
        key = self._make_key(openweb_id, model)
        safe_key = key.replace(":", "_").replace("/", "_")
        return self.file_path.parent / f"{safe_key}.json"

    async def init(self) -> bool:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)

            if self.file_path.exists():
                async with self._lock:
                    with open(self.file_path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                        for key, value in loaded.items():
                            if isinstance(value, dict):
                                self._data[key] = value
                            else:
                                self._data[key] = {
                                    "qwen_chat_id": value,
                                    "last_parent_id": None,
                                    "is_new": False,
                                    "created_at": 0.0
                                }
                logger.info(f"💾 FileBackend loaded {len(self._data)} records from {self.file_path}")
            else:
                logger.info(f"💾 FileBackend initialized with empty state at {self.file_path}")
            return True
        except Exception as e:
            logger.error(f"❌ FileBackend init failed: {e}")
            return False

    async def close(self):
        await self._save()
        self._data.clear()
        logger.debug("💾 FileBackend closed")

    async def _save(self):
        async with self._lock:
            try:
                temp_file = str(self.file_path) + ".tmp"
                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    import os
                    os.fsync(f.fileno())
                self.file_path.parent.mkdir(parents=True, exist_ok=True)
                import os
                os.replace(temp_file, self.file_path)
                logger.debug(f"💾 FileBackend saved {len(self._data)} records")
            except Exception as e:
                logger.error(f"❌ FileBackend save error: {e}")
                raise

    async def get(self, openweb_id: str, model: Optional[str] = None) -> Optional[ChatStateData]:
        loop = asyncio.get_event_loop()
        file_path = self._get_file_path(openweb_id, model)
        
        # Try to read file with composite key
        if await loop.run_in_executor(None, file_path.exists):
            try:
                content = await loop.run_in_executor(None, file_path.read_text)
                data = json.loads(content)
                return ChatStateData.from_dict(data)
            except (json.JSONDecodeError, KeyError):
                return None
        
        # Fallback: check legacy file without model
        if model:
            legacy_path = self._get_file_path(openweb_id, None)
            if await loop.run_in_executor(None, legacy_path.exists):
                try:
                    content = await loop.run_in_executor(None, legacy_path.read_text)
                    data = json.loads(content)
                    return ChatStateData.from_dict(data)
                except:
                    pass
        
        return None

    async def set(self, openweb_id: str, data: ChatStateData, model: Optional[str] = None):
        effective_model = model or data.model
        file_path = self._get_file_path(openweb_id, effective_model)
        
        async with self._lock:
            self._data[file_path.stem] = data.to_dict()
        await self._save()

    async def update_parent(self, openweb_id: str, parent_id: str, model: Optional[str] = None):
        state = await self.get(openweb_id, model)
        if not state:
            return
        
        state.last_parent_id = parent_id
        state.is_new = False
        await self.set(openweb_id, state, model)

    async def delete(self, openweb_id: str, model: Optional[str] = None):
        loop = asyncio.get_event_loop()
        file_path = self._get_file_path(openweb_id, model)
        
        if await loop.run_in_executor(None, file_path.exists):
            await loop.run_in_executor(None, file_path.unlink)
            return True
        return False

    async def health_check(self) -> bool:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            test_file = self.file_path.parent / ".write_test"
            test_file.write_text("test")
            test_file.unlink()
            return True
        except Exception:
            return False
