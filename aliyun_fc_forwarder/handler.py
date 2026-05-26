from __future__ import annotations

import base64
import json
import os
import urllib.request
from typing import Any


def _b64url_encode_json(obj: dict[str, Any]) -> str:
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    out = base64.urlsafe_b64encode(raw).decode("utf-8")
    return out.rstrip("=")


def _http_json(url: str, *, method: str = "POST", headers: dict[str, str] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    h = dict(headers or {})
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h.setdefault("Content-Type", "application/json; charset=utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        obj = {}
    return obj if isinstance(obj, dict) else {}


def _feishu_tenant_token(*, app_id: str, app_secret: str) -> str:
    obj = _http_json(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        body={"app_id": app_id, "app_secret": app_secret},
    )
    token = obj.get("tenant_access_token")
    return str(token) if isinstance(token, str) and token else ""


def _feishu_send_text(*, tenant_token: str, receive_id_type: str, receive_id: str, text: str) -> None:
    url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
    _http_json(
        url,
        headers={"Authorization": f"Bearer {tenant_token}"},
        body={"receive_id": receive_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
    )


def _normalize_runninghub(body: dict[str, Any]) -> dict[str, Any] | None:
    task_id = body.get("taskId")
    if not isinstance(task_id, str) or not task_id.strip():
        return None
    event_data = body.get("eventData")
    if isinstance(event_data, str) and event_data.strip():
        try:
            event_data = json.loads(event_data)
        except Exception:
            event_data = None
    if not isinstance(event_data, dict):
        return None
    code = event_data.get("code")
    ok = True if code in (0, "0", None) else False
    data = event_data.get("data")
    files: list[dict[str, Any]] = []
    if isinstance(data, list):
        for it in data:
            if not isinstance(it, dict):
                continue
            url = it.get("fileUrl") or it.get("url")
            if isinstance(url, str) and url.strip():
                files.append(
                    {
                        "url": str(url).strip(),
                        "fileType": it.get("fileType"),
                        "nodeId": it.get("nodeId"),
                        "filename": it.get("fileName") or it.get("filename") or it.get("name"),
                    }
                )
    return {
        "provider": "runninghub",
        "prompt_id": str(task_id).strip(),
        "status": "success" if ok else "failed",
        "completed": True,
        "files": files,
    }


def _normalize_comfyui(body: dict[str, Any]) -> dict[str, Any] | None:
    pid = body.get("prompt_id") or body.get("promptId")
    if not isinstance(pid, str) or not pid.strip():
        return None
    status = body.get("status")
    completed = body.get("completed")
    ctx0 = body.get("context")
    if isinstance(ctx0, str) and ctx0.strip():
        try:
            ctx0 = json.loads(ctx0)
        except Exception:
            ctx0 = None
    if not isinstance(ctx0, dict):
        ctx0 = {}
    out: dict[str, Any] = {
        "provider": "comfyui",
        "prompt_id": pid.strip(),
        "status": status if isinstance(status, str) else "",
        "completed": bool(completed) if isinstance(completed, bool) else True,
        "context": ctx0,
        "timestamp": body.get("timestamp"),
    }
    return out


def handler(event: Any, context: Any) -> Any:
    token = os.environ.get("WEBHOOK_TOKEN", "").strip()
    cb_sig = os.environ.get("CB_MESSAGE_TOKEN", "").strip()
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    receive_id = os.environ.get("FEISHU_RECEIVE_ID", "").strip()
    at_user_id = os.environ.get("FEISHU_AT_USER_ID", "").strip()

    if not app_id or not app_secret or not receive_id or not at_user_id:
        return {"statusCode": 500, "body": "missing feishu env"}

    if isinstance(event, (bytes, str)):
        try:
            event = json.loads(event)
        except Exception:
            event = {}
    if not isinstance(event, dict):
        event = {}

    q = event.get("queryParameters") or {}
    if not isinstance(q, dict):
        q = {}
    in_token = str(q.get("token") or "").strip()
    if token and in_token != token:
        return {"statusCode": 401, "body": "invalid token"}

    body_raw = event.get("body")
    if isinstance(body_raw, str) and event.get("isBase64Encoded"):
        try:
            body_raw = base64.b64decode(body_raw.encode("utf-8")).decode("utf-8")
        except Exception:
            body_raw = ""
    if isinstance(body_raw, str):
        try:
            body = json.loads(body_raw)
        except Exception:
            body = {}
    elif isinstance(body_raw, dict):
        body = body_raw
    else:
        body = {}

    payload = _normalize_comfyui(body) or _normalize_runninghub(body)
    if not payload:
        return {"statusCode": 400, "body": "unsupported payload"}

    sig_part = f" sig={cb_sig}" if cb_sig else ""
    provider = str(payload.get("provider") or "").strip()
    pid = str(payload.get("prompt_id") or "").strip()
    text = f"/cb provider={provider} id={pid}{sig_part}"
    text = f'{text} <at user_id="{at_user_id}">bot</at>'

    tenant_token = _feishu_tenant_token(app_id=app_id, app_secret=app_secret)
    if not tenant_token:
        return {"statusCode": 500, "body": "feishu auth failed"}
    _feishu_send_text(tenant_token=tenant_token, receive_id_type="chat_id", receive_id=receive_id, text=text)
    return {"statusCode": 200, "body": "ok"}
