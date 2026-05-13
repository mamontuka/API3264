"""
title: Qwen3.6-max Image Editor Fix
description: Fix image extraction from chat for image_edit. Apply for qwen3.6-max LLM model.
author: Oleh Mamont - https://github.com/mamontuka
version: 1.4
license: GPLv3
type: filter
"""

import os
import shutil
import json
from typing import Dict, Any
from pydantic import BaseModel, Field


class Filter:
    def __init__(self):
        self.valves = self.Valves()

    class Valves(BaseModel):
        target_model_prefix: str = Field(
            default="qwen3.6-max",
            description="Модель должна начинаться с этого префикса, чтобы фильтр активировался.",
        )
        storage_path: str = Field(
            default="/root/ai/core/servers/webui/data/uploads/qwen3.6-max-cache",
            description="Директория для временного хранения скопированных файлов.",
        )
        auto_cleanup: bool = Field(
            default=True,
            description="Включить автоматическую очистку старых файлов (задел на будущее).",
        )
        max_files_per_request: int = Field(
            default=10,
            description="Максимальное количество вложений, обрабатываемых за один запрос.",
        )
        inject_format: str = Field(
            default="system_path",
            description="Способ добавления путей к файлам в сообщение.",
            json_schema_extra={
                "type": "select",
                "options": [
                    {
                        "value": "system_path",
                        "label": "System Path ([SYSTEM_INJECTED_PATH])",
                    },
                    {"value": "user_prompt", "label": "User Prompt (Image path:)"},
                    {"value": "json_block", "label": "JSON Block"},
                ],
            },
        )
        log_level: str = Field(
            default="info",
            description="Детализация вывода в логи.",
            json_schema_extra={
                "type": "select",
                "options": [
                    {"value": "debug", "label": "Debug (полный вывод)"},
                    {"value": "info", "label": "Info (основные события)"},
                    {"value": "warning", "label": "Warning (предупреждения и ошибки)"},
                    {"value": "error", "label": "Error (только ошибки)"},
                ],
            },
        )

    async def inlet(
        self,
        body: Dict[str, Any],
        __user__: Dict[str, Any] = None,
        __event_emitter__: callable = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Фильтр с настраиваемыми вальвами для обработки вложений.
        """

        def log(message: str, level: str = "info"):
            levels = {"debug": 0, "info": 1, "warning": 2, "error": 3}
            if levels.get(level, 1) >= levels.get(self.valves.log_level, 1):
                print(f"🔧 QWEN3.6-MAX FILTER [{level.upper()}]: {message}", flush=True)

        log("CALLED")
        model_id = body.get("model", "")
        log(f"model_id='{model_id}'", "debug")

        if not model_id.startswith(self.valves.target_model_prefix):
            log(
                f"SKIPPED (prefix mismatch: expected '{self.valves.target_model_prefix}')"
            )
            return body

        log(f"MATCHED prefix '{self.valves.target_model_prefix}'")
        messages = body.get("messages", [])
        if not messages:
            log("SKIPPED (no messages)")
            return body

        last_msg = messages[-1]
        attachments = last_msg.get("attachments", [])
        log(f"attachments_count={len(attachments)}")

        if not attachments:
            log("SKIPPED (no attachments)")
            return body

        if len(attachments) > self.valves.max_files_per_request:
            log(
                f"WARNING: Too many attachments ({len(attachments)}), limiting to {self.valves.max_files_per_request}",
                "warning",
            )
            attachments = attachments[: self.valves.max_files_per_request]

        injected_paths = []

        try:
            from open_webui.models.files import Files
        except ImportError as e:
            log(f"ERROR: Cannot import Files module: {e}", "error")
            return body

        for idx, att in enumerate(attachments):
            file_id = att.get("id")
            log(f"Processing attachment #{idx}, file_id='{file_id}'", "debug")

            if not file_id:
                log(f"SKIPPED attachment #{idx} (no id)", "debug")
                continue

            try:
                file_record = Files.get_file_by_id(file_id)
                if not file_record:
                    log(f"SKIPPED file_id='{file_id}' (record not found)")
                    continue

                file_path = None
                if hasattr(Files, "get_content_path"):
                    file_path = Files.get_content_path(file_id)
                elif hasattr(file_record, "path"):
                    file_path = file_record.path

                log(f"file_path resolved to '{file_path}'", "debug")

                if not file_path or not os.path.exists(file_path):
                    log(f"SKIPPED file_id='{file_id}' (path invalid or missing)")
                    continue

                os.makedirs(self.valves.storage_path, exist_ok=True)
                filename = getattr(file_record, "filename", f"{file_id}.dat")
                dest_path = os.path.join(
                    self.valves.storage_path, f"{file_id}_{filename}"
                )
                shutil.copy(file_path, dest_path)
                injected_paths.append(dest_path)
                log(f"SUCCESS copied '{file_id}' -> '{dest_path}'")

            except Exception as e:
                log(f"ERROR: Failed to process file_id='{file_id}': {e}", "error")
                continue

        if injected_paths:
            content = last_msg.get("content", "")

            if self.valves.inject_format == "system_path":
                injected_text = "\n" + "\n".join(
                    [f"[SYSTEM_INJECTED_PATH]: {p}" for p in injected_paths]
                )
            elif self.valves.inject_format == "user_prompt":
                injected_text = "\n" + "\n".join(
                    [f"Image path: {p}" for p in injected_paths]
                )
            elif self.valves.inject_format == "json_block":
                injected_text = f'\n```json\n{{"injected_paths": {json.dumps(injected_paths)}}}\n```'
            else:
                injected_text = "\n" + "\n".join(
                    [f"[SYSTEM_INJECTED_PATH]: {p}" for p in injected_paths]
                )

            if isinstance(content, str):
                last_msg["content"] = content + injected_text
            elif isinstance(content, list):
                last_msg["content"].append({"type": "text", "text": injected_text})

            log(f"INJECTED {len(injected_paths)} path(s) into message content")
            log(f"Paths: {injected_paths}", "debug")

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": f"🔧 Qwen3.6-max Fix: Injected {len(injected_paths)} path(s)"
                        },
                    }
                )
        else:
            log("NO PATHS INJECTED (all files skipped or failed)")

        log("DONE")
        return body
