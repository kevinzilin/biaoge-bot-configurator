from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .network import should_trust_env_proxy_for_url


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


def _dump_failed_request(response: httpx.Response, payload: dict[str, Any] | None) -> None:
    """将失败的请求参数和响应写入调试 dump 目录。"""
    try:
        root = Path(os.environ.get("BIAOGE_ROOT") or os.getcwd())
        enabled = str(os.environ.get("SAVE_TASK_REQUEST_PARAMS", "0") or "").strip().lower()
        if enabled not in ("1", "true", "yes", "y", "on"):
            return
        save_dir_path = root / "logs" / "dumps" / "task_requests"
        save_dir_path.mkdir(parents=True, exist_ok=True)
        body_text = ""
        try:
            ct = str(response.headers.get("content-type") or "").lower()
            if "application/json" in ct:
                body_text = json.dumps(response.json(), ensure_ascii=False)
            else:
                body_text = response.text
        except Exception:
            try:
                body_text = response.text
            except Exception:
                body_text = "(failed to read body)"
        dump: dict[str, Any] = {
            "url": str(response.request.url) if response.request else str(response.url),
            "status_code": response.status_code,
            "request_payload": payload,
            "response_body": body_text.strip()[:5000] if body_text else "",
        }
        ts = int(time.time() * 1000)
        wf_name = str(payload.get("workflowName") or payload.get("workflow_name") or "unknown") if payload else "unknown"
        wf_slug = wf_name.replace("/", "_").replace("\\", "_")[:60]
        dump_path = save_dir_path / f"comfyui_error_{wf_slug}_{ts}.json"
        with dump_path.open("w", encoding="utf-8") as f:
            json.dump(dump, f, ensure_ascii=False, indent=2, default=str)
        logging.info("ComfyUI request debug dump written to %s", dump_path)
    except Exception:
        logging.exception("Failed to write ComfyUI request debug dump")


def _read_response_text(response: httpx.Response) -> str:
    try:
        ct = str(response.headers.get("content-type") or "").lower()
        if "application/json" in ct:
            return json.dumps(response.json(), ensure_ascii=False)
        return response.text
    except Exception:
        try:
            return response.text
        except Exception:
            return ""


def _read_response_json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _looks_like_retryable_workflow_payload_error(response: httpx.Response) -> bool:
    status = int(getattr(response, "status_code", 0) or 0)
    if status not in (400, 404, 422):
        return False
    body = _read_response_text(response).lower()
    if not body:
        return False
    # 这些属于“请求已经被插件真正处理过，但业务校验失败”，绝不能继续试第二种 payload，
    # 否则同一个子任务可能被重复提交。
    terminal_markers = (
        "nodeinfolist_invalid",
        "workflow_not_found",
        "prompt_outputs_failed_validation",
        "missing_node_type",
        "node_not_found",
        "custom node may not be installed",
        "the custom node may not be installed",
        "class_type",
        "\"msg\": \"error\"",
        "\"status\": \"error\"",
    )
    if any(marker in body for marker in terminal_markers):
        return False
    if "node '" in body and "not found" in body:
        return False
    # 只有看起来像“字段名/请求外壳不匹配”时，才允许尝试下一种 payload 写法。
    retryable_markers = (
        "field required",
        "workflowname",
        "workflow_name",
        "nodeinfolist",
        "node_info_list",
        "clientid",
        "client_id",
        "unexpected keyword",
        "extra fields not permitted",
        "unrecognized field",
        "invalid request body",
        "invalid payload",
    )
    return any(marker in body for marker in retryable_markers)


class ComfyUIClient:
    def __init__(self, base_url: str, *, upload_timeout_seconds: int = 20) -> None:
        self._base_url = base_url.rstrip("/")
        self._upload_timeout_seconds = max(3, int(upload_timeout_seconds or 20))
        self._trust_env_proxy = should_trust_env_proxy_for_url(self._base_url)

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

        payloads: list[tuple[str, dict[str, Any]]] = []
        p1: dict[str, Any] = {"workflowName": workflow_name, "client_id": client_id}
        if node_list is not None:
            p1["nodeInfoList"] = node_list
        if extra_data is not None:
            p1["extra_data"] = extra_data
        payloads.append(("camel_case", p1))

        p2: dict[str, Any] = {"workflow_name": workflow_name, "client_id": client_id}
        if node_list is not None:
            p2["node_info_list"] = node_list
        if extra_data is not None:
            p2["extra_data"] = extra_data
        payloads.append(("snake_case", p2))

        p3: dict[str, Any] = {"workflowName": workflow_name, "clientId": client_id}
        if node_list is not None:
            p3["nodeInfoList"] = node_list
        if extra_data is not None:
            p3["extraData"] = extra_data
        payloads.append(("legacy_client_id", p3))

        last: httpx.Response | None = None
        last_payload: dict[str, Any] | None = None
        attempts: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=30, trust_env=self._trust_env_proxy) as client:
            for idx, (variant_name, payload) in enumerate(payloads):
                r = await client.post(f"{self._base_url}/prompt_workflow", json=payload)
                last = r
                last_payload = payload
                if 200 <= r.status_code < 300:
                    data = r.json()
                    return ComfyQueued(prompt_id=data.get("prompt_id"), raw=data)
                body_text = _read_response_text(r)
                body_obj = _read_response_json(r)
                attempts.append(
                    {
                        "variant": variant_name,
                        "status_code": r.status_code,
                        "body_text": body_text,
                        "body_obj": body_obj,
                        "prompt_id": (body_obj or {}).get("prompt_id") if isinstance(body_obj, dict) else None,
                    }
                )
                if r.status_code not in (400, 404, 422):
                    try:
                        r.raise_for_status()
                    except httpx.HTTPStatusError as e:
                        setattr(e, "prompt_workflow_attempts", attempts)
                        raise
                if idx < len(payloads) - 1 and _looks_like_retryable_workflow_payload_error(r):
                    logging.warning(
                        "prompt_workflow variant failed but looks retryable, trying next variant: variant=%s status=%s body=%s",
                        variant_name,
                        r.status_code,
                        body_text[:300],
                    )
                    continue
                logging.warning(
                    "prompt_workflow variant failed and will stop retrying: variant=%s status=%s body=%s",
                    variant_name,
                    r.status_code,
                    body_text[:300],
                )
                _dump_failed_request(r, payload)
                try:
                    r.raise_for_status()
                except httpx.HTTPStatusError as e:
                    setattr(e, "prompt_workflow_attempts", attempts)
                    raise
        if last is None:
            raise RuntimeError("queue_workflow failed: no response")
        _dump_failed_request(last, last_payload)
        try:
            last.raise_for_status()
        except httpx.HTTPStatusError as e:
            setattr(e, "prompt_workflow_attempts", attempts)
            raise
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

        timeout = float(self._upload_timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=self._trust_env_proxy) as client:
                with open(file_path, "rb") as f:
                    files = {"image": (name, f, _guess_mime(name))}
                    r = await client.post(f"{self._base_url}/upload/image", data=data, files=files)
                    r.raise_for_status()
                    return r.json()
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"上传图片到 ComfyUI 超时：url={self._base_url}/upload/image，file={name}，timeout={timeout:g}s。"
                "请检查 ComfyUI 是否卡住/不可写 input 目录，或关闭 COMFYUI_UPLOAD_ENABLED 改走本地输入目录。"
            ) from exc

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
        async with httpx.AsyncClient(timeout=30, trust_env=self._trust_env_proxy) as client:
            r = await client.post(
                f"{self._base_url}/prompt",
                content=json.dumps(prompt_payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            if r.status_code < 200 or r.status_code >= 300:
                _dump_failed_request(r, prompt_payload)
            r.raise_for_status()
            data = r.json()
            return ComfyQueued(prompt_id=data.get("prompt_id"), raw=data)

    async def get_queue(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10, trust_env=self._trust_env_proxy) as client:
            r = await client.get(f"{self._base_url}/queue")
            r.raise_for_status()
            return r.json()

    async def interrupt(self) -> None:
        async with httpx.AsyncClient(timeout=10, trust_env=self._trust_env_proxy) as client:
            r = await client.post(f"{self._base_url}/interrupt")
            r.raise_for_status()

    async def get_history(self, *, prompt_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30, trust_env=self._trust_env_proxy) as client:
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
