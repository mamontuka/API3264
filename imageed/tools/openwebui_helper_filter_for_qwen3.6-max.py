"""
title: Qwen3.6-max Image Editor Fix
description: Fix image extraction from chat for image_edit. Apply for qwen3.6-max LLM model.
author: Oleh Mamont
version: 1.1
type: filter
"""

import os
import shutil
from typing import Dict, Any


class Filter:
    def __init__(self):
        self.valves = self.Valves()

    class Valves:
        target_model_prefix: str = "qwen3.6-max"
        storage_path: str = "/root/ai/core/servers/webui/data/uploads/qwen3.6-max-cache"

    async def inlet(
        self,
        body: Dict[str, Any],
        __user__: Dict[str, Any] = None,
        __event_emitter__: callable = None,
        **kwargs,
    ) -> Dict[str, Any]:

        # 1. Start logging
        print(
            "🔧 QWEN3.6-MAX FILTER: ==========================================", flush=True
        )
        print(f"🔧 QWEN3.6-MAX FILTER: CALLED", flush=True)

        model_id = body.get("model", "")
        print(f"🔧 QWEN3.6-MAX FILTER: model_id='{model_id}'", flush=True)

        # 2. Check prefix
        if not model_id.startswith(self.valves.target_model_prefix):
            print(
                f"🔧 QWEN3.6-MAX FILTER: SKIPPED (prefix mismatch: expected '{self.valves.target_model_prefix}')",
                flush=True,
            )
            print(
                "🔧 QWEN3.6-MAX FILTER: ==========================================",
                flush=True,
            )
            return body

        print(
            f"🔧 QWEN3.6-MAX FILTER: MATCHED prefix '{self.valves.target_model_prefix}'",
            flush=True,
        )

        messages = body.get("messages", [])
        if not messages:
            print("🔧 QWEN3.6-MAX FILTER: SKIPPED (no messages)", flush=True)
            return body

        last_msg = messages[-1]
        attachments = last_msg.get("attachments", [])

        # 3. Check attachments
        print(f"🔧 QWEN3.6-MAX FILTER: attachments_count={len(attachments)}", flush=True)

        if not attachments:
            print("🔧 QWEN3.6-MAX FILTER: SKIPPED (no attachments)", flush=True)
            return body

        injected_paths = []

        # Import Files module
        try:
            from open_webui.models.files import Files
        except ImportError as e:
            print(
                f"🔧 QWEN3.6-MAX FILTER ERROR: Cannot import Files module: {e}", flush=True
            )
            return body

        # 4. File Processing
        for idx, att in enumerate(attachments):
            file_id = att.get("id")
            print(
                f"🔧 QWEN3.6-MAX FILTER: Processing attachment #{idx}, file_id='{file_id}'",
                flush=True,
            )

            if not file_id:
                print(
                    f"🔧 QWEN3.6-MAX FILTER: SKIPPED attachment #{idx} (no id)", flush=True
                )
                continue

            try:
                file_record = Files.get_file_by_id(file_id)
                if not file_record:
                    print(
                        f"🔧 QWEN3.6-MAX FILTER: SKIPPED file_id='{file_id}' (record not found)",
                        flush=True,
                    )
                    continue

                # Get path
                file_path = None
                if hasattr(Files, "get_content_path"):
                    file_path = Files.get_content_path(file_id)
                elif hasattr(file_record, "path"):
                    file_path = file_record.path

                print(
                    f"🔧 QWEN3.6-MAX FILTER: file_path resolved to '{file_path}'", flush=True
                )

                if not file_path or not os.path.exists(file_path):
                    print(
                        f"🔧 QWEN3.6-MAX FILTER: SKIPPED file_id='{file_id}' (path invalid or missing)",
                        flush=True,
                    )
                    continue

                # Copying
                os.makedirs(self.valves.storage_path, exist_ok=True)
                filename = getattr(file_record, "filename", f"{file_id}.dat")
                dest_path = os.path.join(
                    self.valves.storage_path, f"{file_id}_{filename}"
                )

                shutil.copy(file_path, dest_path)
                injected_paths.append(dest_path)

                print(
                    f"🔧 QWEN3.6-MAX FILTER: SUCCESS copied '{file_id}' -> '{dest_path}'",
                    flush=True,
                )

            except Exception as e:
                print(
                    f"🔧 QWEN3.6-MAX FILTER ERROR: Failed to process file_id='{file_id}': {e}",
                    flush=True,
                )
                continue

        # 5. Path injection
        if injected_paths:
            content = last_msg.get("content", "")
            injected_text = "\n" + "\n".join(
                [f"[SYSTEM_INJECTED_PATH]: {p}" for p in injected_paths]
            )

            if isinstance(content, str):
                last_msg["content"] = content + injected_text
            elif isinstance(content, list):
                last_msg["content"].append({"type": "text", "text": injected_text})

            print(
                f"🔧 QWEN3.6-MAX FILTER: INJECTED {len(injected_paths)} path(s) into message content",
                flush=True,
            )
            print(f"🔧 QWEN3.6-MAX FILTER: Paths: {injected_paths}", flush=True)

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": f"🔧 QWEN3.6-MAX Fix: Injected {len(injected_paths)} path(s)"
                        },
                    }
                )
        else:
            print(
                "🔧 QWEN3.6-MAX FILTER: NO PATHS INJECTED (all files skipped or failed)",
                flush=True,
            )

        print("🔧 QWEN3.6-MAX FILTER: DONE", flush=True)
        print(
            "🔧 QWEN3.6-MAX FILTER: ==========================================", flush=True
        )

        return body
