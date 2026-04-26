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
    """
    File-based implementation of ChatStateBackend.
    Stores chat mappings in a JSON file with atomic write operations.
    Thread-safe via internal lock.
    """

    def __init__(self, file_path: Path):
        self.file_path = file_path
        self._lock = asyncio.Lock()
        self._data: dict[str, dict] = {}

    async def init(self) -> bool:
        """Load existing state from file if present."""
        try:
            # Ensure parent directory exists
            self.file_path.parent.mkdir(parents=True, exist_ok=True)

            if self.file_path.exists():
                async with self._lock:
                    with open(self.file_path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                        # Convert dict format to internal structure
                        for key, value in loaded.items():
                            if isinstance(value, dict):
                                self._data[key] = value
                            else:
                                # Legacy format fallback
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
        """Save state and release resources."""
        await self._save()
        self._data.clear()
        logger.debug("💾 FileBackend closed")

    async def _save(self):
        """Atomic save to file."""
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

    async def get(self, openweb_id: str) -> Optional[ChatStateData]:
        """Retrieve state for given OpenWebUI chat ID."""
        async with self._lock:
            record = self._data.get(openweb_id)
            if not record:
                return None
            return ChatStateData(
                qwen_chat_id=record.get("qwen_chat_id", ""),
                last_parent_id=record.get("last_parent_id"),
                is_new=record.get("is_new", False),
                created_at=record.get("created_at", 0.0)
            )

    async def set(self, openweb_id: str, data: ChatStateData):
        """Save or update state for given OpenWebUI chat ID."""
        async with self._lock:
            self._data[openweb_id] = {
                "qwen_chat_id": data.qwen_chat_id,
                "last_parent_id": data.last_parent_id,
                "is_new": data.is_new,
                "created_at": data.created_at
            }
        await self._save()

    async def update_parent(self, openweb_id: str, parent_id: str):
        """Update last_parent_id for existing chat."""
        async with self._lock:
            if openweb_id in self._data:
                self._data[openweb_id]["last_parent_id"] = parent_id
                self._data[openweb_id]["is_new"] = False
        await self._save()

    async def delete(self, openweb_id: str):
        """Delete state for given OpenWebUI chat ID."""
        async with self._lock:
            self._data.pop(openweb_id, None)
        await self._save()

    async def health_check(self) -> bool:
        """Check if file is writable."""
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            test_file = self.file_path.parent / ".write_test"
            test_file.write_text("test")
            test_file.unlink()
            return True
        except Exception:
            return False
