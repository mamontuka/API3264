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
Base classes and interfaces for Chat State backends.
Defines the contract that all storage implementations must follow.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class ChatStateData:
    """
    Data structure representing chat state mapping.
    Used uniformly across all backend implementations.
    """
    qwen_chat_id: str
    last_parent_id: Optional[str] = None
    is_new: bool = False
    created_at: float = 0.0

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "qwen_chat_id": self.qwen_chat_id,
            "last_parent_id": self.last_parent_id,
            "is_new": self.is_new,
            "created_at": self.created_at
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ChatStateData":
        """Create instance from dictionary."""
        return cls(
            qwen_chat_id=data.get("qwen_chat_id", ""),
            last_parent_id=data.get("last_parent_id"),
            is_new=data.get("is_new", False),
            created_at=data.get("created_at", 0.0)
        )


class ChatStateBackend(ABC):
    """
    Abstract base class for chat state storage backends.
    All implementations (File, PostgreSQL) must inherit this.
    """

    @abstractmethod
    async def init(self) -> bool:
        """Initialize backend resources. Returns True on success."""
        pass

    @abstractmethod
    async def close(self):
        """Release backend resources."""
        pass

    @abstractmethod
    async def get(self, openweb_id: str) -> Optional[ChatStateData]:
        """Retrieve state for given OpenWebUI chat ID."""
        pass

    @abstractmethod
    async def set(self, openweb_id: str, data: ChatStateData):
        """Save or update state for given OpenWebUI chat ID."""
        pass

    @abstractmethod
    async def update_parent(self, openweb_id: str, parent_id: str):
        """Update last_parent_id for existing chat."""
        pass

    @abstractmethod
    async def delete(self, openweb_id: str):
        """Delete state for given OpenWebUI chat ID."""
        pass

    async def health_check(self) -> bool:
        """
        Check backend availability.
        Default implementation returns True; override if needed.
        """
        return True
