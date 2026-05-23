# Copyright (C) 2026
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

import json, logging
from pathlib import Path
from typing import List
from .base import TokenBackend, TokenData

logger = logging.getLogger(__name__)

class FileTokenBackend(TokenBackend):
    def __init__(self, file_path: Path):
        self.file_path = file_path
        self._tokens: List[TokenData] = []

    async def init(self) -> bool:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            if self.file_path.exists():
                with open(self.file_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                    self._tokens = [TokenData.from_dict(t) for t in raw]
            else:
                self._tokens = []
            return True
        except Exception as e:
            logger.error(f"FileTokenBackend init failed: {e}")
            return False

    async def close(self): pass

    async def load_all(self) -> List[TokenData]:
        return self._tokens

    async def save_all(self, tokens: List[TokenData]):
        self._tokens = tokens
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump([t.to_dict() for t in tokens], f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"FileTokenBackend save failed: {e}")

    async def clear(self):
        self._tokens = []
        if self.file_path.exists():
            self.file_path.unlink()
            logger.info(f"Cleared tokens file: {self.file_path}")
