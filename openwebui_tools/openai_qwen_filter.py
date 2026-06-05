"""
title: Adapter openai-qwen
description: Sends only the last user message to reduce context sent into qwen
author: Oleh Mamont
version: 1.0
type: filter
"""

from typing import Optional, List
from pydantic import BaseModel


class Valves(BaseModel):
    pipelines: List[str] = ["*"]
    priority: int = 0


class Filter:
    def __init__(self):
        self.type = "filter"
        self.name = "Last Message Only"
        self.valves = Valves()

    async def inlet(self, body: dict, user: Optional[dict] = None) -> dict:
        messages = body.get("messages", [])
        if len(messages) > 1:
            system_msgs = [m for m in messages if m.get("role") == "system"]
            last_user_msg = next(
                (m for m in reversed(messages) if m.get("role") == "user"), None
            )
            if last_user_msg:
                body["messages"] = system_msgs + [last_user_msg]
        return body
