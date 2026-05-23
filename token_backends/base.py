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

"""
Base classes for Token storage backends.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

@dataclass
class TokenData:
    id: str
    token: str
    cookies: List[Dict[str, Any]] = field(default_factory=list)
    added_at: str = ""
    invalid: bool = False
    resetAt: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "token": self.token,
            "cookies": self.cookies,
            "added_at": self.added_at,
            "invalid": self.invalid,
            "resetAt": self.resetAt
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TokenData":
        return cls(
            id=data.get("id", ""),
            token=data.get("token", ""),
            cookies=data.get("cookies", []),
            added_at=data.get("added_at", ""),
            invalid=data.get("invalid", False),
            resetAt=data.get("resetAt")
        )

class TokenBackend(ABC):
    @abstractmethod
    async def init(self) -> bool: pass

    @abstractmethod
    async def close(self): pass

    @abstractmethod
    async def load_all(self) -> List[TokenData]: pass

    @abstractmethod
    async def save_all(self, tokens: List[TokenData]): pass

    @abstractmethod
    async def clear(self): pass

    async def health_check(self) -> bool:
        return True
