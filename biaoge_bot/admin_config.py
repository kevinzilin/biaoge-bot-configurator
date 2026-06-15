from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .context import AppContext
from .config import resolve_project_path

_LOCK = threading.RLock()

_SENSITIVE_KEY_RE = re.compile(r"(SECRET|TOKEN|PASSWORD|PASS|API_KEY|APP_SECRET|PRIVATE)", re.IGNORECASE)
_SENSITIVE_KEYS = {
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "CB_MESSAGE_TOKEN",
    "RUNNINGHUB_API_KEY",
    "ADMIN_TOKEN",
}
_HIDDEN_ENV_KEYS = {
    "BITABLE_APP_TOKEN",
    "BITABLE_TABLE_ID",
    "BITABLE_APP_TOKEN_KLEIN",
    "BITABLE_TABLE_ID_KLEIN",
    "BITABLE_MODE",
    "BIAOGE_LICENSE_PATH",
    "WORKFLOW_CONFIG_PATH",
    "CALLBACK_PATH",
}

_BOOL_ENV_KEYS = {
    "CALLBACK_DUMP_ENABLED",
    "SAVE_TASK_REQUEST_PARAMS",
    "COMFYUI_UPLOAD_ENABLED",
    "FEISHU_SEND_RESULT_TO_CHAT",
}
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
_ENV_SCHEMA: dict[str, dict[str, Any]] = {
    "CALLBACK_HOST": {
        "group": "基础服务",
        "order": 10,
        "type": "text",
        "default": "127.0.0.1",
        "description": "本机/局域网可访问的监听地址。示例：127.0.0.1 或 192.168.x.x。",
    },
    "CALLBACK_PORT": {
        "group": "基础服务",
        "order": 20,
        "type": "text",
        "default": "9901",
        "description": "回调服务端口，配置页也通过该端口访问。",
    },
    "BOT_LOG_LEVEL": {
        "group": "日志与调试",
        "order": 30,
        "type": "select",
        "options": ["DEBUG", "INFO", "WARNING", "ERROR"],
        "default": "INFO",
        "description": "运行日志等级。排查问题时可临时改为 DEBUG。",
        "readonlyHint": "运行日志固定写入 logs/biaoge_bot-YYYY-MM-DD.log。",
    },
    "CALLBACK_DUMP_ENABLED": {
        "group": "日志与调试",
        "order": 40,
        "type": "switch",
        "default": "0",
        "description": "是否保存回调 payload 调试 dump。",
        "readonlyHint": "开启后固定写入 logs/dumps/callbacks。",
    },
    "SAVE_TASK_REQUEST_PARAMS": {
        "group": "日志与调试",
        "order": 50,
        "type": "switch",
        "default": "0",
        "description": "是否保存任务请求参数、workflow JSON、extra_data 等调试 dump。",
        "readonlyHint": "开启后固定写入 logs/dumps/task_requests。",
    },
    "FEISHU_SEND_RESULT_TO_CHAT": {
        "group": "基础服务",
        "order": 55,
        "type": "switch",
        "default": "0",
        "description": "绑定表格的任务完成后，是否也把生成结果发送回触发的飞书对话框。",
    },
    "COMFYUI_BASE_URL": {
        "group": "ComfyUI",
        "order": 60,
        "type": "text",
        "default": "http://127.0.0.1:8188",
        "description": "ComfyUI 服务地址。",
    },
    "COMFYUI_INPUT_DIR": {
        "group": "ComfyUI",
        "order": 70,
        "type": "text",
        "default": "",
        "description": "ComfyUI 输入目录。留空表示不指定。",
    },
    "COMFYUI_UPLOAD_ENABLED": {
        "group": "ComfyUI",
        "order": 80,
        "type": "switch",
        "default": "0",
        "description": "是否允许通过 ComfyUI /upload/image 上传图片。",
    },
    "COMFYUI_UPLOAD_SUBFOLDER": {
        "group": "ComfyUI",
        "order": 90,
        "type": "text",
        "default": "",
        "description": "上传到 ComfyUI 的子目录。留空表示不指定。",
    },
    "COMFYUI_UPLOAD_OVERWRITE": {
        "group": "ComfyUI",
        "order": 100,
        "type": "text",
        "default": "true",
        "description": "上传同名文件时是否覆盖。",
    },
    "RESULT_OUTPUT_DIR": {
        "group": "ComfyUI",
        "order": 110,
        "type": "text",
        "default": "",
        "description": "结果输出目录。留空则只做临时中转不落盘。",
    },
    "REMOTE_CALLBACK_URL": {
        "group": "远程回调",
        "order": 120,
        "type": "text",
        "default": "",
        "description": "公网/远程回调地址。适用于外部服务可回调到指定地址的情况。",
    },
    "REMOTE_RESULT_MODE": {
        "group": "远程回调",
        "order": 130,
        "type": "text",
        "default": "poll",
        "description": "远程结果获取模式，例如 poll/fc。",
    },
    "REMOTE_POLL_INTERVAL_SECONDS": {
        "group": "远程回调",
        "order": 140,
        "type": "text",
        "default": "60",
        "description": "远程轮询间隔秒数。",
    },
    "REMOTE_POLL_FALLBACK_SECONDS": {
        "group": "远程回调",
        "order": 150,
        "type": "text",
        "default": "600",
        "description": "远程轮询兜底超时秒数。",
    },
    "RUNNINGHUB_API_KEY": {
        "group": "RunningHub",
        "order": 160,
        "type": "text",
        "default": "",
        "description": "RunningHub API Key。密钥类配置不会在页面展示。",
    },
}
_ENV_GROUP_ORDER = ["基础服务", "日志与调试", "ComfyUI", "远程回调", "RunningHub", "其它"]


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _env_path() -> Path:
    return _repo_root() / ".env"


def _workflow_path() -> Path:
    root = _repo_root()
    env_p = _env_path()
    try:
        _, values = _read_env_file(env_p)
        raw = str(values.get("WORKFLOW_CONFIG_PATH") or "").strip()
        raw = raw.strip('"').strip("'")
        if raw:
            p = Path(resolve_project_path(raw, root=root))
            try:
                p.relative_to(root.resolve())
                return p
            except Exception:
                pass
    except Exception:
        pass
    return root / "config" / "workflows.local.json"


def _is_sensitive_key(key: str) -> bool:
    k = (key or "").strip()
    if not k:
        return True
    if k in _SENSITIVE_KEYS:
        return True
    return bool(_SENSITIVE_KEY_RE.search(k))


def _is_hidden_env_key(key: str) -> bool:
    k = (key or "").strip()
    if not k:
        return True
    return k in _HIDDEN_ENV_KEYS


def _normalize_bool_value(value: Any) -> str:
    v = str(value or "").strip().lower()
    return "1" if v in ("1", "true", "yes", "y", "on") else "0"


def _normalize_env_value(key: str, value: Any) -> str:
    if key in _BOOL_ENV_KEYS:
        return _normalize_bool_value(value)
    if key == "BOT_LOG_LEVEL":
        level = str(value or "").strip().upper()
        if level not in _LOG_LEVELS:
            raise HTTPException(status_code=400, detail="BOT_LOG_LEVEL must be one of DEBUG/INFO/WARNING/ERROR")
        return level
    return "" if value is None else str(value)


def _visible_env_values(values: dict[str, str]) -> dict[str, str]:
    bitable_mode = str(values.get("BITABLE_MODE") or "").strip().lower()
    visible = {k: v for k, v in values.items() if not _is_sensitive_key(k) and not _is_hidden_env_key(k)}
    if bitable_mode == "off":
        visible.pop("FEISHU_SEND_RESULT_TO_CHAT", None)
    for key, meta in _ENV_SCHEMA.items():
        if _is_sensitive_key(key) or _is_hidden_env_key(key):
            continue
        if bitable_mode == "off" and key == "FEISHU_SEND_RESULT_TO_CHAT":
            continue
        visible.setdefault(key, str(meta.get("default", "")))
    return visible


def _visible_env_schema(values: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    values = values or {}
    bitable_mode = str(values.get("BITABLE_MODE") or "").strip().lower()
    out: dict[str, dict[str, Any]] = {}
    for key, meta in _ENV_SCHEMA.items():
        if _is_sensitive_key(key) or _is_hidden_env_key(key):
            continue
        if bitable_mode == "off" and key == "FEISHU_SEND_RESULT_TO_CHAT":
            continue
        out[key] = dict(meta)
    return out


def _read_env_file(path: Path) -> tuple[list[str], dict[str, str]]:
    if not path.exists():
        return [], {}
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    values: dict[str, str] = {}
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = val.strip()
    return lines, values


def _write_env_file(path: Path, updates: dict[str, str]) -> None:
    lines, existing = _read_env_file(path)
    existing_keys = set(existing.keys())

    def render_value(v: Any) -> str:
        if v is None:
            return ""
        return str(v)

    out_lines: list[str] = []
    for line in lines:
        if "=" not in line or line.lstrip().startswith("#"):
            out_lines.append(line)
            continue
        key, _ = line.split("=", 1)
        k = key.strip()
        if k in updates:
            out_lines.append(f"{k}={render_value(updates[k])}")
        else:
            out_lines.append(line)

    for k, v in updates.items():
        kk = str(k).strip()
        if not kk or kk in existing_keys:
            continue
        out_lines.append(f"{kk}={render_value(v)}")

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_workflow_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8") or "{}")


def _write_workflow_config(path: Path, cfg: dict[str, Any]) -> None:
    bak = path.with_name(path.name + ".bak")
    if path.exists():
        bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _require_admin(req: Request) -> None:
    token = (os.environ.get("ADMIN_TOKEN") or "").strip()
    if token:
        provided = (req.headers.get("x-admin-token") or req.query_params.get("token") or "").strip()
        if provided != token:
            raise HTTPException(status_code=401, detail="unauthorized")
        return
    host = getattr(req.client, "host", "") if req.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=401, detail="unauthorized")


def _reload_context_inplace(ctx: AppContext) -> None:
    from .main import build_context

    env_file = str(_env_path())
    new_ctx = build_context(env_file=env_file)
    with _LOCK:
        for k in (
            "settings",
            "config",
            "auth",
            "bitables",
            "drive",
            "comfyui",
            "workflows",
            "bitable_mode",
            "bitable_configs",
            "default_table_key",
            "default_workflow_key",
        ):
            object.__setattr__(ctx, k, getattr(new_ctx, k))
        try:
            ctx.runner.set_context(ctx)
        except Exception:
            pass


_ADMIN_STATIC_DIR = Path(__file__).with_name("admin_static")


def _admin_asset_version() -> str:
    mtimes: list[int] = []
    for name in ("admin.html", "admin.css", "admin.js"):
        try:
            mtimes.append(int((_ADMIN_STATIC_DIR / name).stat().st_mtime))
        except OSError:
            continue
    return str(max(mtimes) if mtimes else 0)


def _admin_page_html() -> str:
    path = _ADMIN_STATIC_DIR / "admin.html"
    try:
        html = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"admin static page missing: {path}") from exc
    return html.replace("{{ASSET_VERSION}}", _admin_asset_version())


def register_admin(app: FastAPI, ctx: AppContext) -> None:
    if not any(getattr(route, "path", None) == "/admin/static" for route in app.routes):
        app.mount("/admin/static", StaticFiles(directory=str(_ADMIN_STATIC_DIR)), name="admin_static")

    @app.get("/admin/config", dependencies=[Depends(_require_admin)])
    async def admin_config_page() -> HTMLResponse:
        return HTMLResponse(
            _admin_page_html(),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
            },
        )

    @app.get("/admin/api/env", dependencies=[Depends(_require_admin)])
    async def admin_get_env() -> dict[str, Any]:
        path = _env_path()
        with _LOCK:
            _, values = _read_env_file(path)
        bitable_mode = str(values.get("BITABLE_MODE") or "").strip().lower()
        visible = _visible_env_values(values)
        return {
            "values": visible,
            "path": str(path),
            "meta": {
                "bitable_mode": bitable_mode,
                "env_schema": _visible_env_schema(values),
                "env_group_order": list(_ENV_GROUP_ORDER),
            },
        }

    @app.put("/admin/api/env", dependencies=[Depends(_require_admin)])
    async def admin_put_env(payload: dict[str, Any] = Body(default_factory=dict), reload: int = 0) -> dict[str, Any]:
        raw = payload.get("values") or {}
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="invalid values")
        updates: dict[str, str] = {}
        for k, v in raw.items():
            if not isinstance(k, str) or not k.strip():
                continue
            kk = k.strip()
            if _is_sensitive_key(kk) or _is_hidden_env_key(kk):
                continue
            updates[kk] = _normalize_env_value(kk, v)
        path = _env_path()
        with _LOCK:
            _write_env_file(path, updates)
        if reload:
            _reload_context_inplace(ctx)
        return {"ok": True}

    @app.get("/admin/api/workflows", dependencies=[Depends(_require_admin)])
    async def admin_get_workflows() -> dict[str, Any]:
        path = _workflow_path()
        with _LOCK:
            cfg = _read_workflow_config(path)
        return {"config": cfg, "path": str(path), "admin_token_missing": not bool((os.environ.get("ADMIN_TOKEN") or "").strip())}

    @app.put("/admin/api/workflows", dependencies=[Depends(_require_admin)])
    async def admin_put_workflows(payload: dict[str, Any] = Body(default_factory=dict), reload: int = 0) -> dict[str, Any]:
        cfg = payload.get("config")
        if not isinstance(cfg, dict):
            raise HTTPException(status_code=400, detail="invalid config")
        try:
            json.dumps(cfg)
        except Exception:
            raise HTTPException(status_code=400, detail="config not serializable")
        path = _workflow_path()
        with _LOCK:
            _write_workflow_config(path, cfg)
        if reload:
            _reload_context_inplace(ctx)
        return {"ok": True}

    @app.post("/admin/api/reload", dependencies=[Depends(_require_admin)])
    async def admin_reload() -> dict[str, Any]:
        _reload_context_inplace(ctx)
        return {"ok": True}
