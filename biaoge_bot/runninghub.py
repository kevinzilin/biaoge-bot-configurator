from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx


_TASK_QUEUE_MAXED = "TASK_QUEUE_MAXED"
_DEFAULT_TASK_QUEUE_MAXED_RETRIES = 5
_DEFAULT_TASK_QUEUE_RETRY_DELAY_SECONDS = 15.0
_MAX_TASK_QUEUE_RETRY_DELAY_SECONDS = 60.0


def _strip_ticks(s: str) -> str:
    v = (s or "").strip()
    if v.startswith("`") and v.endswith("`") and len(v) >= 2:
        v = v[1:-1].strip()
    return v


def _runninghub_code(obj: dict[str, Any]) -> int | None:
    code = obj.get("code")
    if isinstance(code, int):
        return code
    if isinstance(code, str):
        s = code.strip()
        if s:
            try:
                return int(s)
            except ValueError:
                return None
    return None


def _is_task_queue_maxed(obj: dict[str, Any]) -> bool:
    return _runninghub_code(obj) == 421


def _runninghub_error_message(obj: dict[str, Any]) -> str:
    for source in (obj, obj.get("data") if isinstance(obj.get("data"), dict) else {}):
        for key in ("message", "msg", "errorMessage", "errorCode", "code"):
            v = source.get(key)
            if v is not None and str(v).strip():
                return str(v).strip()
    return f"runninghub error: {obj}"


@dataclass(frozen=True)
class RunningHubCreated:
    task_id: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class RunningHubQueryV2:
    task_id: str | None
    status: str | None
    error_code: str | None
    error_message: str | None
    results: list[dict[str, Any]]
    raw: dict[str, Any]


@dataclass(frozen=True)
class RunningHubUploaded:
    type: str | None
    download_url: str | None
    file_name: str | None
    size: str | None
    raw: dict[str, Any]


class RunningHubClient:
    def __init__(self, *, api_key: str, base_url: str = "https://www.runninghub.cn") -> None:
        self._api_key = (api_key or "").strip()
        self._base_url = (base_url or "").rstrip("/")

    async def create_task(
        self,
        *,
        workflow_id: str,
        node_info_list: list[dict[str, Any]] | None = None,
        webhook_url: str | None = None,
        add_metadata: bool | None = None,
        workflow: str | None = None,
        instance_type: str | None = None,
        use_personal_queue: bool | None = None,
        retain_seconds: int | None = None,
        access_password: str | None = None,
        task_queue_maxed_retries: int = _DEFAULT_TASK_QUEUE_MAXED_RETRIES,
        task_queue_maxed_retry_delay_seconds: float = _DEFAULT_TASK_QUEUE_RETRY_DELAY_SECONDS,
    ) -> RunningHubCreated:
        if not self._api_key:
            raise RuntimeError("missing RUNNINGHUB_API_KEY")
        wid = str(workflow_id or "").strip()
        if not wid:
            raise RuntimeError("missing runninghub workflowId")

        payload: dict[str, Any] = {"apiKey": self._api_key, "workflowId": wid}
        if node_info_list is not None:
            payload["nodeInfoList"] = node_info_list
        if webhook_url:
            payload["webhookUrl"] = _strip_ticks(str(webhook_url).strip())
        if workflow:
            payload["workflow"] = workflow
        if instance_type:
            payload["instanceType"] = instance_type
        if access_password:
            payload["accessPassword"] = access_password
        if add_metadata is not None:
            payload["addMetadata"] = bool(add_metadata)
        if use_personal_queue is not None:
            payload["usePersonalQueue"] = bool(use_personal_queue)
        if retain_seconds is not None:
            payload["retainSeconds"] = int(retain_seconds)

        headers = {
            "Host": "www.runninghub.cn",
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        max_retries = max(0, int(task_queue_maxed_retries))
        retry_delay = max(0.0, float(task_queue_maxed_retry_delay_seconds))
        async with httpx.AsyncClient(timeout=30) as client:
            for attempt in range(max_retries + 1):
                r = await client.post(
                    f"{self._base_url}/task/openapi/create",
                    content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers=headers,
                )
                r.raise_for_status()
                obj = r.json()
                if not isinstance(obj, dict):
                    raise RuntimeError("runninghub response invalid")
                code = _runninghub_code(obj)
                is_task_queue_maxed = _is_task_queue_maxed(obj)
                if code != 0:
                    if is_task_queue_maxed and attempt < max_retries:
                        sleep_seconds = min(
                            _MAX_TASK_QUEUE_RETRY_DELAY_SECONDS,
                            retry_delay * (2 ** attempt) if retry_delay else 0.0,
                        )
                        logging.warning(
                            "runninghub create_task queue maxed, retrying: workflow_id=%s attempt=%s/%s delay=%ss error=%s",
                            wid,
                            attempt + 1,
                            max_retries + 1,
                            sleep_seconds,
                            _runninghub_error_message(obj),
                        )
                        if sleep_seconds > 0:
                            await asyncio.sleep(sleep_seconds)
                        continue
                    raise RuntimeError(_runninghub_error_message(obj))
                data = obj.get("data") or {}
                task_id = None
                if isinstance(data, dict):
                    tid = data.get("taskId")
                    if isinstance(tid, str) and tid.strip():
                        task_id = tid.strip()
                    elif isinstance(tid, int):
                        task_id = str(tid)
                return RunningHubCreated(task_id=task_id, raw=obj)

        raise RuntimeError("runninghub create_task retry exhausted")

    async def query_results_v2(self, *, task_id: str) -> RunningHubQueryV2:
        if not self._api_key:
            raise RuntimeError("missing RUNNINGHUB_API_KEY")
        tid = str(task_id or "").strip()
        if not tid:
            raise RuntimeError("missing taskId")

        headers = {
            "Host": "www.runninghub.cn",
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self._base_url}/openapi/v2/query",
                content=json.dumps({"taskId": tid}, ensure_ascii=False).encode("utf-8"),
                headers=headers,
            )
            r.raise_for_status()
            obj = r.json()
            if not isinstance(obj, dict):
                raise RuntimeError("runninghub response invalid")

            out_task_id = obj.get("taskId")
            out_task_id = str(out_task_id).strip() if isinstance(out_task_id, str) and out_task_id.strip() else tid
            status = obj.get("status")
            status = str(status).strip() if isinstance(status, str) and status.strip() else None
            error_code = obj.get("errorCode")
            error_code = str(error_code).strip() if isinstance(error_code, str) and error_code.strip() else None
            error_message = obj.get("errorMessage")
            error_message = str(error_message).strip() if isinstance(error_message, str) and error_message.strip() else None
            results0 = obj.get("results")
            results: list[dict[str, Any]] = []
            if isinstance(results0, list):
                for it in results0:
                    if isinstance(it, dict):
                        results.append(it)

            return RunningHubQueryV2(
                task_id=out_task_id,
                status=status,
                error_code=error_code,
                error_message=error_message,
                results=results,
                raw=obj,
            )

    async def upload_media_binary(self, *, file_path: str) -> RunningHubUploaded:
        if not self._api_key:
            raise RuntimeError("missing RUNNINGHUB_API_KEY")
        p = str(file_path or "").strip()
        if not p:
            raise RuntimeError("missing file_path")

        headers = {
            "Host": "www.runninghub.cn",
            "Authorization": f"Bearer {self._api_key}",
        }
        async with httpx.AsyncClient(timeout=60) as client:
            with open(p, "rb") as f:
                r = await client.post(
                    f"{self._base_url}/openapi/v2/media/upload/binary",
                    headers=headers,
                    files={"file": f},
                )
            r.raise_for_status()
            obj = r.json()
            if not isinstance(obj, dict):
                raise RuntimeError("runninghub response invalid")
            code = obj.get("code")
            if code not in (0, "0", None):
                raise RuntimeError(str(obj.get("message") or obj.get("msg") or f"runninghub error: {obj}"))
            data = obj.get("data") or {}
            if not isinstance(data, dict):
                data = {}
            t = data.get("type")
            download_url = data.get("download_url")
            file_name = data.get("fileName")
            size = data.get("size")
            return RunningHubUploaded(
                type=str(t).strip() if isinstance(t, str) and t.strip() else None,
                download_url=str(download_url).strip() if isinstance(download_url, str) and download_url.strip() else None,
                file_name=str(file_name).strip() if isinstance(file_name, str) and file_name.strip() else None,
                size=str(size).strip() if isinstance(size, str) and size.strip() else None,
                raw=obj,
            )
