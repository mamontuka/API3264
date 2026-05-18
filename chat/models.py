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
MODULE: CHAT MODELS
Model mapping and listing.
"""
import logging
from typing import List

from config import Config

logger = logging.getLogger(__name__)


def get_mapped_model(model_name: str) -> str:
    """
    Get the actual Qwen model name for a given alias.
    Allows users to request models by friendly names (e.g., "qwen-max")
    while the proxy translates to the actual API model name (e.g., "qwen3.5-plus").
    Args:
        model_name: Model name from client request (case-insensitive)
    Returns:
        str: Mapped model name if found in Config.MODEL_MAPPING, else original name
    """
    return Config.MODEL_MAPPING.get(model_name.lower(), model_name)


def load_available_models() -> List[str]:
    """
    Load list of available models from configuration and file.
    Combines:
    1. Models defined in Config.MODEL_MAPPING keys
    2. Default model from Config.DEFAULT_MODEL
    3. Additional models listed in Config.AVAILABLE_MODELS_FILE (one per line)
    Returns:
        List[str]: Sorted list of available model names
    """
    models = set(Config.MODEL_MAPPING.keys())
    models.add(Config.DEFAULT_MODEL)
    # Load additional models from file if it exists
    if Config.AVAILABLE_MODELS_FILE.exists():
        try:
            with open(Config.AVAILABLE_MODELS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    value = line.strip()
                    # Skip empty lines and comments
                    if value and not value.startswith("#"):
                        models.add(value)
        except Exception as e:
            logger.warning(f"Failed to load models from {Config.AVAILABLE_MODELS_FILE}: {e}")
    return sorted(models)
