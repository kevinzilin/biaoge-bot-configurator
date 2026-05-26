from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx


def _guess_mime(filename: str) -> str:
    low = filename.lower()
    if low.endswith(".png"):
        return "image/png"
    if low.endswith(".jpg") or low.endswith(".jpeg"):
        return "image/jpeg"
    if low.endswith(".webp"):
        return "image/webp"
    if low.endswith(".gif"):
        return "image/gif"
    return "application/octet-stream"


@dataclass(frozen=True)
class ComfyQueued:
    prompt_id: str | None
    raw: dict[str, Any]


class ComfyUIClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    async def queue_workflow(
        self,
        *,
        workflow_name: str,
        node_info_list: list[dict[str, Any]] | None = None,
        extra_data: dict[str, Any] | None = None,
        client_id: str = "biaoge-bot",
    ) -> ComfyQueued:
        def normalize_node_info_list(items: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
            if items is None:
                return None
            out: list[dict[str, Any]] = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                node_id = it.get("nodeId")
                if isinstance(node_id, str) and node_id.strip().isdigit():
                    node_id = int(node_id.strip())
                field_name = it.get("fieldName")
                field_value = it.get("fieldValue")
                out.append({"nodeId": node_id, "fieldName": field_name, "fieldValue": field_value})
            return out

        node_list = normalize_node_info_list(node_info_list)

        payloads: list[dict[str, Any]] = []
        p1: dict[str, Any] = {"workflowName": workflow_name, "client_id": client_id}
        if node_list is not None:
            p1["nodeInfoList"] = node_list
        if extra_data is not None:
            p1["extra_data"] = extra_data
        payloads.append(p1)

        p2: dict[str, Any] = {"workflow_name": workflow_name, "client_id": client_id}
        if node_list is not None:
            p2["node_info_list"] = node_list
        if extra_data is not None:
            p2["extra_data"] = extra_data
        payloads.append(p2)

        p3: dict[str, Any] = {"workflowName": workflow_name, "clientId": client_id}
        if node_list is not None:
            p3["nodeInfoList"] = node_list
        if extra_data is not None:
            p3["extraData"] = extra_data
        payloads.append(p3)

        last: httpx.Response | None = None
        async with httpx.AsyncClient(timeout=30) as client:
            for payload in payloads:
                r = await client.post(f"{self._base_url}/prompt_workflow", json=payload)
                last = r
                if 200 <= r.status_code < 300:
                    data = r.json()
                    return ComfyQueued(prompt_id=data.get("prompt_id"), raw=data)
                if r.status_code not in (400, 404, 422):
                    r.raise_for_status()
        if last is None:
            raise RuntimeError("queue_workflow failed: no response")
        last.raise_for_status()
        raise RuntimeError("queue_workflow failed")

    async def upload_image(
        self,
        *,
        file_path: str,
        filename: str | None = None,
        type: str = "input",
        overwrite: bool = True,
        subfolder: str | None = None,
    ) -> dict[str, Any]:
        name = filename or file_path.split("\\")[-1].split("/")[-1]
        data: dict[str, Any] = {"type": type, "overwrite": "true" if overwrite else "false"}
        if subfolder:
            data["subfolder"] = subfolder

        async with httpx.AsyncClient(timeout=60) as client:
            with open(file_path, "rb") as f:
                files = {"image": (name, f, _guess_mime(name))}
                r = await client.post(f"{self._base_url}/upload/image", data=data, files=files)
                r.raise_for_status()
                return r.json()

    async def queue_api_prompt(
        self,
        *,
        prompt: dict[str, Any],
        extra_data: dict[str, Any] | None = None,
        client_id: str = "biaoge-bot",
    ) -> ComfyQueued:
        payload: dict[str, Any] = {"prompt": prompt, "client_id": client_id}
        if extra_data is not None:
            payload["extra_data"] = extra_data
        return await self.queue_prompt(payload)

    async def queue_prompt(self, prompt_payload: dict[str, Any]) -> ComfyQueued:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self._base_url}/prompt",
                content=json.dumps(prompt_payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            r.raise_for_status()
            data = r.json()
            return ComfyQueued(prompt_id=data.get("prompt_id"), raw=data)

    async def get_queue(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self._base_url}/queue")
            r.raise_for_status()
            return r.json()

    async def interrupt(self) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{self._base_url}/interrupt")
            r.raise_for_status()

    async def get_history(self, *, prompt_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{self._base_url}/history/{prompt_id}")
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                return data
            return {}

    async def get_history_item(self, *, prompt_id: str) -> dict[str, Any] | None:
        data = await self.get_history(prompt_id=prompt_id)
        if not isinstance(data, dict):
            return None
        item = data.get(prompt_id)
        if isinstance(item, dict):
            return item
        return None
