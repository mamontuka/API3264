"""
title: Image Vision & Edit Capability
description: Marker tool that informs the model about vision capabilities. Actual image processing happens automatically at proxy level.
author: Oleh Mamont
version: 2.0
license: GPLv3
"""

from pydantic import BaseModel, Field
from typing import Any, Awaitable, Callable, Dict, List, Optional


class Tools:
    class Valves(BaseModel):
        pass

    def __init__(self):
        self.valves = self.Valves()

    async def image_vision(
        self,
        prompt: str,
        image_url: str = "",
        __event_emitter__: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
        __messages__: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        You have vision capabilities! You can see and edit images automatically.

        When user provides an image:
        - You can analyze, describe, or answer questions about it
        - You can edit/modify the image based on instructions
        - Image processing happens automatically via vision model
        - Just call this tool with your instruction

        Args:
            prompt: What you want to do with the image (describe, edit, analyze, etc.)
            image_url: Optional (auto-extracted from context if not provided)

        Returns:
            The vision model will process the image and return result automatically.
        """
        # This tool is just a marker — actual processing happens at proxy level
        # The proxy detects images and automatically routes to vision model
        return "Vision capability activated. Processing image..."
