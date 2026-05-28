from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.parse import urlencode

import httpx
from fastapi import Body, Depends, FastAPI, File, Form, Request, UploadFile

from .admin_config import _require_admin, register_admin
from .comfyui import ComfyUIClient
from .context import AppContext
from .im import IMClient
from .modules.bitable_logic import resolve_relation_prompts as _enc_resolve_relation_prompts
from .modules.bitable_writeback import (
    update_runlog_record as _enc_update_runlog_record,
    update_split_progress_and_maybe_finalize as _enc_update_split_progress_and_maybe_finalize,
    write_back_record as _enc_write_back_record,
)

_DONE_NOTIFIED: dict[str, float] = {}
_DONE_NOTIFIED_LOCK = asyncio.Lock()
_FIELDS_META_CACHE: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
_FIELDS_META_CACHE_LOCK = asyncio.Lock()
_SPLIT_PROGRESS: dict[tuple[str, str, str], dict[str, Any]] = {}
_SPLIT_PROGRESS_LOCK = asyncio.Lock()
_SPLIT_SUMMARY_NOTIFIED: dict[tuple[str, str, str], float] = {}
_SPLIT_SUMMARY_NOTIFIED_LOCK = asyncio.Lock()


def _is_file_path(v: str) -> bool:
    try:
        p = Path(v)
    except Exception:
        return False
    return p.is_file()


def _guess_is_image(path: str) -> bool:
    ext = Path(path).suffix.lower().lstrip(".")
    return ext in {"png", "jpg", "jpeg", "webp", "gif", "bmp"}


def _is_http_url(v: str) -> bool:
    s = (v or "").strip()
    if not s:
        return False
    try:
        p = urlparse(s)
    except Exception:
        return False
    return p.scheme in {"http", "https"} and bool(p.netloc)


def _collect_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: list[str] = []
        for x in value:
            out.extend(_collect_strings(x))
        return out
    if isinstance(value, dict):
        out: list[str] = []
        for x in value.values():
            out.extend(_collect_strings(x))
        return out
    return [str(value)]


def _collect_display_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        return [s] if s else []
    if isinstance(value, (int, float, bool)):
        return [str(value)]
    if isinstance(value, list):
        out: list[str] = []
        for x in value:
            out.extend(_collect_display_strings(x))
        return out
    if isinstance(value, dict):
        for k in ("text", "name", "title", "label", "display_value", "displayValue", "value"):
            v = value.get(k)
            if isinstance(v, str):
                s = v.strip()
                if s:
                    return [s]
            elif isinstance(v, (int, float, bool)):
                return [str(v)]
            elif isinstance(v, (list, dict)):
                got = _collect_display_strings(v)
                if got:
                    return got
        out2: list[str] = []
        for vv in value.values():
            out2.extend(_collect_display_strings(vv))
        return out2
    s2 = str(value).strip()
    return [s2] if s2 else []


def _bitable_status_snapshot(ctx: AppContext) -> dict[str, Any]:
    try:
        mode = getattr(ctx, "bitable_mode", None)
        mode_obj = {
            "read_enabled": bool(getattr(mode, "read_enabled", False)),
            "write_enabled": bool(getattr(mode, "write_enabled", False)),
        }
    except Exception:
        mode_obj = {"read_enabled": False, "write_enabled": False}
    keys = sorted([str(k) for k in (getattr(ctx, "bitables", None) or {}).keys() if k])
    cfg_keys = sorted([str(k) for k in (getattr(ctx, "bitable_configs", None) or {}).keys() if k])
    default_key = str(getattr(ctx, "default_table_key", "") or "")
    return {"mode": mode_obj, "default_table_key": default_key, "bitable_keys": keys, "configured_table_keys": cfg_keys}


def _extract_callback_context(payload: dict[str, Any]) -> dict[str, Any]:
    for k in ("extra_data", "extraData"):
        extra = payload.get(k)
        if isinstance(extra, str) and extra.strip():
            try:
                extra = json.loads(extra)
            except Exception:
                extra = None
        if isinstance(extra, dict):
            ctx = extra.get("callback_context") or extra.get("callbackContext")
            if isinstance(ctx, dict):
                return ctx

    ctx = payload.get("context")
    if isinstance(ctx, str) and ctx.strip():
        try:
            ctx = json.loads(ctx)
        except Exception:
            ctx = None
    if isinstance(ctx, dict):
        return ctx

    for k in ("callback_context", "callbackContext"):
        ctx = payload.get(k)
        if isinstance(ctx, str) and ctx.strip():
            try:
                ctx = json.loads(ctx)
            except Exception:
                ctx = None
        if isinstance(ctx, dict):
            return ctx
    return {}


def _extract_output_candidates(payload: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for k in ("outputs", "output", "files", "file_paths", "filePaths", "fileUrl", "fileURL", "file_url", "url"):
        candidates.extend(_collect_strings(payload.get(k)))

    data = payload.get("data")
    if isinstance(data, dict):
        for k in ("outputs", "files", "file_paths"):
            candidates.extend(_collect_strings(data.get(k)))

    result = payload.get("result")
    if isinstance(result, dict):
        for k in ("outputs", "files", "file_paths", "fileUrl", "fileURL", "file_url", "url"):
            candidates.extend(_collect_strings(result.get(k)))

    return [c for c in candidates if isinstance(c, str) and c.strip()]


def _iter_url_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for k in ("files", "data", "outputs"):
        v = payload.get(k)
        if isinstance(v, list):
            for it in v:
                if isinstance(it, dict) and any(x in it for x in ("url", "fileUrl", "fileURL", "file_url")):
                    items.append(it)
    result = payload.get("result")
    if isinstance(result, dict):
        for k in ("files", "data"):
            v = result.get(k)
            if isinstance(v, list):
                for it in v:
                    if isinstance(it, dict) and any(x in it for x in ("url", "fileUrl", "fileURL", "file_url")):
                        items.append(it)
    return items

def _strip_ticks(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("`") and s.endswith("`") and len(s) >= 2:
        s = s[1:-1].strip()
    return s


def _safe_filename(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "output"
    return re.sub(r"[\\/:*?\"<>|\r\n]+", "_", name)


def _iter_output_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    outputs = result.get("outputs")
    if not isinstance(outputs, dict):
        return []
    items: list[dict[str, Any]] = []
    for _, node_out in outputs.items():
        if not isinstance(node_out, dict):
            continue
        for key in ("images", "gifs", "videos", "files"):
            arr = node_out.get(key)
            if not isinstance(arr, list):
                continue
            for it in arr:
                if isinstance(it, dict):
                    items.append(it)
    return items


def _build_view_url(*, base_url: str, filename: str, subfolder: str, type_: str) -> str:
    base = _strip_ticks(base_url).rstrip("/")
    q: dict[str, Any] = {"filename": filename, "type": type_ or "output"}
    if subfolder:
        q["subfolder"] = subfolder
    return f"{base}/view?{urlencode(q)}"


def _collect_result_urls(payload: dict[str, Any], *, base_url: str) -> list[str]:
    urls: list[str] = []
    for it in _iter_output_items(payload):
        u = it.get("fileUrl") or it.get("fileURL") or it.get("file_url") or it.get("url")
        if isinstance(u, str) and _is_http_url(u):
            urls.append(u.strip())
            continue
        filename = it.get("filename")
        if isinstance(filename, str) and filename.strip() and _strip_ticks(base_url).strip():
            subfolder = it.get("subfolder") if isinstance(it.get("subfolder"), str) else ""
            type_ = it.get("type") if isinstance(it.get("type"), str) else "output"
            urls.append(_build_view_url(base_url=base_url, filename=filename.strip(), subfolder=subfolder, type_=type_))
    for it in _iter_url_items(payload):
        u = it.get("fileUrl") or it.get("fileURL") or it.get("file_url") or it.get("url")
        if isinstance(u, str) and _is_http_url(u):
            urls.append(u.strip())
    out: list[str] = []
    seen: set[str] = set()
    for u in urls:
        s = str(u or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


async def _download_from_view(ctx: AppContext, *, base_url: str, filename: str, subfolder: str, type_: str, prompt_id: str | None) -> str | None:
    base = _strip_ticks(base_url).rstrip("/")
    if not base:
        base = ctx.settings.comfyui_base_url.rstrip("/")
    if not base:
        return None
    save_dir = ctx.settings.bitable_download_dir
    os.makedirs(save_dir, exist_ok=True)
    name = _safe_filename(filename)
    prefix = _safe_filename(prompt_id or "prompt")
    out_path = str(Path(save_dir) / f"{prefix}_{name}")
    params: dict[str, Any] = {"filename": filename, "type": type_ or "output"}
    if subfolder:
        params["subfolder"] = subfolder
    async with httpx.AsyncClient(timeout=180) as client:
        content: bytes | None = None
        for i in range(6):
            r = await client.get(f"{base}/view", params=params)
            if r.status_code == 404 and i < 5:
                await asyncio.sleep(0.6 * (i + 1))
                continue
            r.raise_for_status()
            content = r.content
            break
        if content is None:
            return None
        Path(out_path).write_bytes(content)
    return out_path


async def _download_from_url(ctx: AppContext, *, url: str, prompt_id: str | None, filename: str | None = None) -> str | None:
    u = (url or "").strip()
    if not _is_http_url(u):
        return None
    save_dir = ctx.settings.bitable_download_dir
    os.makedirs(save_dir, exist_ok=True)
    name = (filename or "").strip()
    if not name:
        try:
            p = urlparse(u)
            base = unquote((p.path or "").rstrip("/").split("/")[-1])
            name = base
        except Exception:
            name = ""
    if not name:
        name = "output"
    name = _safe_filename(name)
    prefix = _safe_filename(prompt_id or "prompt")
    out_path = str(Path(save_dir) / f"{prefix}_{name}")
    async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
        r = await client.get(u)
        r.raise_for_status()
        Path(out_path).write_bytes(r.content)
    return out_path


async def _resolve_record_by_prompt_id(ctx: AppContext, *, prompt_id: str) -> tuple[str | None, str | None]:
    pid = str(prompt_id or "").strip()
    if not pid:
        return None, None
    if not ctx.bitable_mode.read_enabled:
        return None, None
    for table_key, bitable in (ctx.bitables or {}).items():
        cfg = ctx.bitable_configs.get(table_key) if ctx.bitable_configs else None
        if not cfg:
            continue
        prompt_field = cfg.fields.get("prompt_id")
        if not prompt_field:
            continue
        try:
            items = await bitable.search_records(
                filter_={
                    "conjunction": "and",
                    "conditions": [{"field_name": prompt_field, "operator": "is", "value": [pid]}],
                },
                page_size=1,
            )
        except Exception:
            items = []
        if not items:
            try:
                items = await bitable.search_records(
                    filter_={
                        "conjunction": "and",
                        "conditions": [{"field_name": prompt_field, "operator": "contains", "value": [pid]}],
                    },
                    page_size=1,
                )
            except Exception:
                items = []
        if items:
            rid = items[0].get("record_id")
            if rid:
                return str(rid), str(table_key)
    return None, None


def _extract_error(payload: dict[str, Any]) -> str | None:
    for k in ("error", "err"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for k in ("errorMessage", "error_message"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            code = payload.get("errorCode") or payload.get("error_code")
            cs = str(code).strip() if isinstance(code, (str, int)) and str(code).strip() else ""
            return f"{v.strip()}{(' (code=' + cs + ')') if cs else ''}"
    fr = payload.get("failedReason")
    if isinstance(fr, dict):
        parts: list[str] = []
        node_name = fr.get("node_name") or fr.get("nodeName")
        node_id = fr.get("node_id") or fr.get("nodeId")
        exc_type = fr.get("exception_type") or fr.get("exceptionType")
        exc_msg = fr.get("exception_message") or fr.get("exceptionMessage")
        tb = fr.get("traceback")
        if node_name or node_id:
            parts.append(f"node={node_name or ''}{('#' + str(node_id)) if node_id is not None else ''}".strip())
        if exc_type:
            parts.append(str(exc_type))
        if exc_msg:
            parts.append(str(exc_msg))
        if isinstance(tb, str) and tb.strip():
            parts.append(tb.strip())
        if parts:
            return "\n".join(parts)
    for k in ("message", "msg"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            low = v.strip().lower()
            if low in ("success", "ok"):
                continue
            return v.strip()
    return None


def _resolve_table_key(ctx: AppContext, cb_ctx: dict[str, Any]) -> str | None:
    table_key = cb_ctx.get("tableKey")
    if isinstance(table_key, str) and table_key in ctx.bitable_configs:
        return table_key

    app_token = cb_ctx.get("appToken")
    table_id = cb_ctx.get("tableId")
    if isinstance(app_token, str) and isinstance(table_id, str):
        for k, c in ctx.bitable_configs.items():
            if c.app_token == app_token and c.table_id == table_id:
                return k
    return None


async def _write_back_record(
    *,
    bitable: Any,
    table_cfg: Any,
    workflow_cfg: dict[str, Any] | None,
    record_id: str,
    ok: bool,
    file_tokens: list[dict[str, Any]],
    result_urls: list[str],
    prompt_id: str | None,
    error: str | None,
    cb_ctx: dict[str, Any] | None,
) -> None:
    await _enc_write_back_record(
        bitable=bitable,
        table_cfg=table_cfg,
        workflow_cfg=workflow_cfg,
        record_id=record_id,
        ok=ok,
        file_tokens=file_tokens,
        result_urls=result_urls,
        prompt_id=prompt_id,
        error=error,
        cb_ctx=cb_ctx,
    )


async def _update_split_progress_and_maybe_finalize(
    *,
    bitable: Any | None,
    table_cfg: Any | None,
    workflow_cfg: dict[str, Any] | None,
    table_key: str,
    record_id: str,
    ok: bool,
    cb_ctx: dict[str, Any],
) -> dict[str, Any] | None:
    return await _enc_update_split_progress_and_maybe_finalize(
        bitable=bitable,
        table_cfg=table_cfg,
        workflow_cfg=workflow_cfg,
        table_key=table_key,
        record_id=record_id,
        ok=ok,
        cb_ctx=cb_ctx,
    )


def create_callback_app(ctx: AppContext) -> FastAPI:
    app = FastAPI()

    def _extract_token(req: Request, payload: dict[str, Any]) -> str:
        q = req.query_params.get("token") or req.query_params.get("sig") or ""
        if isinstance(q, str) and q.strip():
            return q.strip()
        for k in ("token", "sig", "signature"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    def _check_token(req: Request, payload: dict[str, Any]) -> bool:
        expected = str(ctx.settings.cb_message_token or "").strip()
        if not expected:
            return True
        got = _extract_token(req, payload)
        return bool(got and got == expected)

    @app.post(ctx.settings.callback_path)
    async def callback(req: Request) -> dict[str, Any]:
        payload = await req.json()
        return await handle_callback_payload(ctx, payload)

    @app.post("/_local/exec")
    async def local_exec(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        from .commands import parse_message_text
        from .dispatcher import TriggerContext, build_panel_card, get_help_text, run_workflow, _reset_table_records

        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "error": "missing text"}
        cmd = parse_message_text(text)
        if not cmd:
            return {"ok": False, "error": "invalid command"}

        name = cmd.name
        args = cmd.args
        trig = TriggerContext(chat_id=None, user_open_id=None, source="local.exec")

        if name == "panel":
            return {"ok": True, "card": build_panel_card(ctx)}

        if name in ("help", "h"):
            return {"ok": True, "text": get_help_text()}

        if name in ("reset", "reset_table"):
            table_key = str(args.get("table") or args.get("tableKey") or args.get("table_key") or ctx.default_table_key or "")
            bitable = ctx.bitables.get(table_key) if table_key else None
            if not bitable:
                return {"ok": False, "error": "missing table"}
            scope = str(args.get("scope") or "all_nonqueued").strip()
            clear = str(args.get("clear") or "").strip().lower() in ("1", "true", "yes", "y", "on")
            n, cp, ce, co, fc, sp, se, so = await _reset_table_records(bitable, scope=scope, clear=clear)
            return {
                "ok": True,
                "reset": {
                    "count": n,
                    "cleared_prompt": cp,
                    "cleared_error": ce,
                    "cleared_output": co,
                    "failed_clear": fc,
                    "skipped_readonly_prompt": sp,
                    "skipped_readonly_error": se,
                    "skipped_readonly_output": so,
                },
            }

        if name == "run_default":
            record_id = None
            table_key = ctx.default_table_key
            bitable = ctx.bitables.get(table_key) if table_key else None
            if bitable and bitable.mode.read_enabled:
                record_id = await bitable.find_next_queued_record_id()
                
            workflow_key = "default"
            if not ctx.workflows.get("default"):
                if table_key:
                    for k, wf_spec in ctx.workflows._specs.items():
                        wf_cfg = (ctx.config.get("workflows") or {}).get(k) or {}
                        if isinstance(wf_cfg, dict) and wf_cfg.get("table") == table_key:
                            workflow_key = k
                            break
                if workflow_key == "default":
                    first_wf = ctx.workflows.first()
                    if first_wf:
                        workflow_key = first_wf.key
                    
            prompt_id = await run_workflow(
                ctx,
                trigger=trig,
                workflow_key=workflow_key,
                record_id=record_id,
                row=None,
                view_id=None,
                params={},
                table_key=table_key,
            )
            return {"ok": True, "prompt_id": prompt_id, "record_id": record_id, "table": table_key, "workflow": workflow_key}

        if name in ("run", "wf"):
            record_id = args.get("record") or args.get("record_id") or args.get("recordId")
            record_id = str(record_id) if record_id else None
            row = args.get("row") or args.get("row_no") or args.get("rowNo") or args.get("line")
            row = int(str(row)) if row is not None and str(row).isdigit() else None
            view_id = args.get("view") or args.get("view_id") or args.get("viewId")
            view_id = str(view_id) if view_id else None
            table_key = args.get("table") or args.get("tableKey") or args.get("table_key")
            table_key = str(table_key) if table_key else None
            if name == "wf":
                wf = args.get("workflow") or args.get("workflowName") or args.get("wf") or args.get("name")
            else:
                wf = args.get("workflow") or "default"
            workflow_key = str(wf) if wf else "default"
            resolved_table_key = table_key or ctx.default_table_key
            cfg_for_wf = (ctx.config.get("workflows") or {}).get(workflow_key) or {}
            if not table_key and isinstance(cfg_for_wf, dict):
                tk = cfg_for_wf.get("table")
                if isinstance(tk, str) and tk:
                    resolved_table_key = tk
            
            if workflow_key == "default" and not ctx.workflows.get("default"):
                if resolved_table_key:
                    for k, wf_spec in ctx.workflows._specs.items():
                        wf_cfg = (ctx.config.get("workflows") or {}).get(k) or {}
                        if isinstance(wf_cfg, dict) and wf_cfg.get("table") == resolved_table_key:
                            workflow_key = k
                            break
                if workflow_key == "default":
                    first_wf = ctx.workflows.first()
                    if first_wf:
                        workflow_key = first_wf.key
                cfg_for_wf = (ctx.config.get("workflows") or {}).get(workflow_key) or {}
                if not table_key and isinstance(cfg_for_wf, dict):
                    tk = cfg_for_wf.get("table")
                    if isinstance(tk, str) and tk:
                        resolved_table_key = tk
                        
            if not record_id and not row:
                bitable = ctx.bitables.get(resolved_table_key) if resolved_table_key else None
                if bitable and bitable.mode.read_enabled:
                    record_id = await bitable.find_next_queued_record_id()
            if not record_id and row:
                bitable = ctx.bitables.get(resolved_table_key) if resolved_table_key else None
                table_cfg = ctx.bitable_configs.get(resolved_table_key) if resolved_table_key else None
                if bitable and table_cfg and bitable.mode.read_enabled:
                    resolved_view_id = view_id or table_cfg.view_id
                    page_token: str | None = None
                    offset = 0
                    while True:
                        items, page_token, has_more, _ = await bitable.list_records_page(
                            view_id=resolved_view_id,
                            page_size=200,
                            page_token=page_token,
                        )
                        if not items:
                            break
                        idx = row - 1
                        if idx >= offset and idx < offset + len(items):
                            picked = items[idx - offset]
                            rid = picked.get("record_id")
                            if rid:
                                record_id = str(rid)
                            break
                        offset += len(items)
                        if not has_more or not page_token:
                            break
                        
            params = {k: v for k, v in (args or {}).items() if k not in ("record", "record_id", "recordId", "row", "row_no", "rowNo", "line", "view", "view_id", "viewId", "workflow", "workflowName", "wf", "name", "table", "tableKey", "table_key")}
            prompt_id = await run_workflow(
                ctx,
                trigger=trig,
                workflow_key=workflow_key,
                record_id=record_id,
                row=row,
                view_id=view_id,
                params=params,
                table_key=table_key,
            )
            return {"ok": True, "prompt_id": prompt_id, "record_id": record_id, "row": row, "table": resolved_table_key, "workflow": workflow_key}

        if name in ("batch", "drain"):
            table_key = str(args.get("table") or args.get("tableKey") or args.get("table_key") or ctx.default_table_key or "")
            workflow_key = str(args.get("workflow") or args.get("workflowName") or args.get("wf") or args.get("name") or "")
            
            if not workflow_key:
                workflow_key = "default"
                if not ctx.workflows.get("default"):
                    if table_key:
                        for k, wf_spec in ctx.workflows._specs.items():
                            wf_cfg = (ctx.config.get("workflows") or {}).get(k) or {}
                            if isinstance(wf_cfg, dict) and wf_cfg.get("table") == table_key:
                                workflow_key = k
                                break
                    if workflow_key == "default":
                        first_wf = ctx.workflows.first()
                        if first_wf:
                            workflow_key = first_wf.key

            if not workflow_key or (workflow_key == "default" and not ctx.workflows.get("default")):
                return {"ok": False, "error": "missing workflow"}
            if not table_key:
                return {"ok": False, "error": "missing table"}
                
            batch = int(str(args.get("batch") or args.get("limit") or "10"))
            inflight = int(str(args.get("inflight") or "1"))
            
            bitable = ctx.bitables.get(table_key)
            if bitable is None or not bitable.mode.read_enabled:
                err_msg = "无法读取表格，无法执行批量(batch/drain)操作。"
                return {"ok": False, "error": err_msg}
                
            await ctx.runner.start(
                workflow_key=workflow_key,
                table_key=table_key,
                batch=batch,
                inflight=inflight,
                drain=(name == "drain"),
                chat_id=None,
            )
            return {"ok": True, "started": {"workflow": workflow_key, "table": table_key}}

        if name == "stop_queue":
            workflow_key = str(args.get("workflow") or args.get("workflowName") or args.get("wf") or args.get("name") or "")
            table_key = str(args.get("table") or args.get("tableKey") or args.get("table_key") or ctx.default_table_key or "")
            
            if not workflow_key:
                workflow_key = "default"
                if not ctx.workflows.get("default"):
                    if table_key:
                        for k, wf_spec in ctx.workflows._specs.items():
                            wf_cfg = (ctx.config.get("workflows") or {}).get(k) or {}
                            if isinstance(wf_cfg, dict) and wf_cfg.get("table") == table_key:
                                workflow_key = k
                                break
                    if workflow_key == "default":
                        first_wf = ctx.workflows.first()
                        if first_wf:
                            workflow_key = first_wf.key

            if workflow_key and table_key:
                await ctx.runner.stop(workflow_key=workflow_key, table_key=table_key)
                return {"ok": True, "stopped": {"workflow": workflow_key, "table": table_key}}
            return {"ok": False, "error": "missing workflow/table"}

        return {"ok": False, "error": f"unsupported command: {name}"}

    @app.post("/_event/exec")
    async def event_exec(req: Request, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        if not _check_token(req, payload):
            return {"ok": False, "error": "invalid token"}

        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            return await local_exec({"text": text.strip()})

        ev = None
        for k in ("event_id", "eventId", "event", "eventKey", "EventKey", "key", "id"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                ev = v.strip()
                break
        if not ev:
            return {"ok": False, "error": "missing event_id"}

        if ev.startswith("cmd_"):
            ev = ev[4:].strip()
        if ev.startswith("/"):
            return await local_exec({"text": ev})
        if "__" in ev:
            parts = [p for p in ev.split("__") if p]
            if parts:
                cmd_text = "/" + parts[0] + ((" " + " ".join(parts[1:])) if len(parts) > 1 else "")
                return await local_exec({"text": cmd_text})
        if ":" in ev:
            parts2 = [p for p in ev.split(":") if p]
            if parts2:
                cmd_text2 = "/" + parts2[0] + ((" " + " ".join(parts2[1:])) if len(parts2) > 1 else "")
                return await local_exec({"text": cmd_text2})
        return await local_exec({"text": f"/{ev}"})

    @app.get("/_local/bitable/fields")
    async def local_bitable_fields(table: str | None = None) -> dict[str, Any]:
        table_key = str(table or ctx.default_table_key or "")
        bitable = ctx.bitables.get(table_key) if table_key else None
        if not bitable:
            return {"ok": False, "error": "missing table", "status": _bitable_status_snapshot(ctx)}
        if not hasattr(bitable, "list_fields"):
            return {"ok": False, "error": "list_fields not supported"}
        try:
            items = await bitable.list_fields()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "table": table_key, "items": items}

    @app.get("/_local/bitable/record")
    async def local_bitable_record(table: str | None = None, record_id: str | None = None) -> dict[str, Any]:
        table_key = str(table or ctx.default_table_key or "")
        bitable = ctx.bitables.get(table_key) if table_key else None
        if not bitable:
            return {"ok": False, "error": "missing table", "status": _bitable_status_snapshot(ctx)}
        if not record_id:
            return {"ok": False, "error": "missing record_id"}
        try:
            rec = await bitable.get_record(str(record_id))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "table": table_key, "record": rec}

    @app.get("/_local/bitable/records")
    async def local_bitable_records(
        table: str | None = None,
        view_id: str | None = None,
        page_size: int = 20,
        page_token: str | None = None,
        fields: str | None = None,
    ) -> dict[str, Any]:
        table_key = str(table or ctx.default_table_key or "")
        bitable = ctx.bitables.get(table_key) if table_key else None
        if not bitable:
            return {"ok": False, "error": "missing table", "status": _bitable_status_snapshot(ctx)}
        if not hasattr(bitable, "list_records_page"):
            return {"ok": False, "error": "list_records_page not supported"}

        ps = int(page_size) if isinstance(page_size, int) else 20
        ps = max(1, min(200, ps))
        vid = str(view_id).strip() if isinstance(view_id, str) and view_id.strip() else None
        pt = str(page_token).strip() if isinstance(page_token, str) and page_token.strip() else None

        want_fields: list[str] = []
        if isinstance(fields, str) and fields.strip():
            want_fields = [x.strip() for x in fields.split(",") if x.strip()]

        try:
            items, next_token, has_more, total = await bitable.list_records_page(view_id=vid, page_size=ps, page_token=pt)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        out_items: list[dict[str, Any]] = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            rid = it.get("record_id")
            f0 = it.get("fields") if isinstance(it.get("fields"), dict) else {}
            if not want_fields:
                out_items.append({"record_id": rid, "fields": f0})
                continue
            raw_selected: dict[str, Any] = {}
            display_selected: dict[str, Any] = {}
            for fn in want_fields:
                rv = f0.get(fn)
                raw_selected[fn] = rv
                dv = _collect_display_strings(rv)
                display_selected[fn] = dv if len(dv) != 1 else dv[0]
            out_items.append({"record_id": rid, "raw": raw_selected, "display": display_selected})

        return {
            "ok": True,
            "table": table_key,
            "view_id": vid,
            "page_size": ps,
            "page_token": pt,
            "next_page_token": next_token,
            "has_more": bool(has_more),
            "total": int(total or 0),
            "items": out_items,
        }

    @app.get("/_local/bitable/relation_prompt")
    async def local_bitable_relation_prompt(
        workflow: str,
        record_id: str,
        table: str | None = None,
    ) -> dict[str, Any]:
        wk = str(workflow or "").strip()
        rid = str(record_id or "").strip()
        if not wk:
            return {"ok": False, "error": "missing workflow"}
        if not rid:
            return {"ok": False, "error": "missing record_id"}

        raw_cfg = (ctx.config.get("workflows") or {}).get(wk) or {}
        if not isinstance(raw_cfg, dict):
            return {"ok": False, "error": "workflow config not found"}
        relation_prompt = raw_cfg.get("relationPrompt") or raw_cfg.get("relation_prompt")
        if not isinstance(relation_prompt, dict):
            return {"ok": False, "error": "workflow missing relationPrompt"}

        resolved_table_key = str(table or raw_cfg.get("table") or ctx.default_table_key or "").strip()
        bitable = ctx.bitables.get(resolved_table_key) if resolved_table_key else None
        if not bitable:
            return {"ok": False, "error": "missing table", "status": _bitable_status_snapshot(ctx)}

        try:
            rec = await bitable.get_record(rid)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        fields0 = rec.get("fields") if isinstance(rec, dict) and isinstance(rec.get("fields"), dict) else {}

        src_field = relation_prompt.get("sourceField") or relation_prompt.get("source_field")
        src_field = str(src_field).strip() if isinstance(src_field, str) and str(src_field).strip() else ""
        if not src_field:
            return {"ok": False, "error": "relationPrompt missing sourceField"}
        src_val = fields0.get(src_field)

        tgt_key = relation_prompt.get("targetTableKey") or relation_prompt.get("target_table_key")
        tgt_key = str(tgt_key).strip() if isinstance(tgt_key, str) and str(tgt_key).strip() else None
        tgt_app = relation_prompt.get("targetAppToken") or relation_prompt.get("target_app_token") or relation_prompt.get("app_token")
        tgt_app = str(tgt_app).strip() if isinstance(tgt_app, str) and str(tgt_app).strip() else None
        tgt_tid = relation_prompt.get("targetTableId") or relation_prompt.get("target_table_id") or relation_prompt.get("table_id")
        tgt_tid = str(tgt_tid).strip() if isinstance(tgt_tid, str) and str(tgt_tid).strip() else None
        tgt_match = relation_prompt.get("targetMatchField") or relation_prompt.get("target_match_field")
        tgt_match = str(tgt_match).strip() if isinstance(tgt_match, str) and str(tgt_match).strip() else None

        pf = relation_prompt.get("promptFields") or relation_prompt.get("prompt_fields") or []
        prompt_fields: list[str] = []
        if isinstance(pf, list):
            for x in pf:
                if isinstance(x, str) and x.strip():
                    prompt_fields.append(x.strip())
        elif isinstance(pf, str) and pf.strip():
            prompt_fields = [pf.strip()]
        if not prompt_fields:
            return {"ok": False, "error": "relationPrompt missing promptFields"}

        join_with = relation_prompt.get("joinWith") or relation_prompt.get("join_with") or "\n"
        join_with = str(join_with) if isinstance(join_with, str) else "\n"
        max_items = relation_prompt.get("maxItems") or relation_prompt.get("max_items") or 20
        max_items = int(max_items) if isinstance(max_items, int) else (int(str(max_items)) if str(max_items).strip().isdigit() else 20)
        max_items = max(1, min(100, max_items))
        strict = relation_prompt.get("strict")
        strict = True if strict is None else bool(strict)

        try:
            prompts = await _enc_resolve_relation_prompts(
                ctx,
                source_value=src_val,
                target_app_token=tgt_app,
                target_table_id=tgt_tid,
                target_table_key=tgt_key,
                target_match_field=tgt_match,
                prompt_fields=prompt_fields,
                join_with=join_with,
                max_items=max_items,
                strict=strict,
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}

        return {
            "ok": True,
            "workflow": wk,
            "table": resolved_table_key,
            "record_id": rid,
            "source": {"field": src_field, "raw": src_val, "display": _collect_display_strings(src_val)},
            "target": {"table_key": tgt_key, "table_id": tgt_tid, "match_field": tgt_match, "prompt_fields": prompt_fields, "join_with": join_with},
            "resolved": {"count": len(prompts), "items": prompts},
        }

    @app.get("/_local/workflow/preview")
    async def local_workflow_preview(
        workflow: str,
        record_id: str | None = None,
        row: int | None = None,
        view_id: str | None = None,
        table: str | None = None,
        resolve_files: bool = False,
    ) -> dict[str, Any]:
        from .dispatcher import TriggerContext, preview_workflow_runs

        wk = str(workflow or "").strip()
        if not wk:
            return {"ok": False, "error": "missing workflow"}
        rid = str(record_id or "").strip() if isinstance(record_id, str) else None
        r = int(row) if isinstance(row, int) else None
        vid = str(view_id).strip() if isinstance(view_id, str) and str(view_id).strip() else None
        tk = str(table or "").strip() if isinstance(table, str) and str(table).strip() else None

        trig = TriggerContext(chat_id=None, user_open_id=None, source="local.workflow.preview")
        try:
            obj = await preview_workflow_runs(
                ctx,
                trigger=trig,
                workflow_key=wk,
                record_id=rid,
                row=r,
                view_id=vid,
                params={},
                table_key=tk,
                resolve_files=bool(resolve_files),
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "preview": obj}

    @app.get("/_local/bitable/status")
    async def local_bitable_status() -> dict[str, Any]:
        return {"ok": True, "status": _bitable_status_snapshot(ctx)}

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    @app.post("/api/upload", dependencies=[Depends(_require_admin)])
    async def api_upload(
        file: UploadFile = File(...),
        provider: str = Form("comfyui"),
        type: str = Form("input"),
        overwrite: str = Form("true"),
        subfolder: str = Form(""),
    ) -> dict[str, Any]:
        prov = str(provider or "").strip().lower() or "comfyui"
        fname = str(getattr(file, "filename", "") or "upload.bin")
        ctype = str(getattr(file, "content_type", "") or "application/octet-stream")

        f0 = getattr(file, "file", None)
        if f0 is None:
            return {"ok": False, "error": "missing file"}
        try:
            f0.seek(0)
        except Exception:
            pass

        if prov == "runninghub":
            api_key = str(getattr(ctx.settings, "runninghub_api_key", "") or "").strip()
            if not api_key:
                return {"ok": False, "error": "missing RUNNINGHUB_API_KEY"}

            headers = {"Host": "www.runninghub.cn", "Authorization": f"Bearer {api_key}"}
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(
                    "https://www.runninghub.cn/openapi/v2/media/upload/binary",
                    headers=headers,
                    files={"file": (fname, f0, ctype)},
                )
            r.raise_for_status()
            obj = r.json()
            if not isinstance(obj, dict):
                return {"ok": False, "error": "runninghub response invalid"}
            code = obj.get("code")
            if code not in (0, "0", None):
                return {"ok": False, "error": str(obj.get("message") or obj.get("msg") or f"runninghub error: {obj}")}
            data = obj.get("data") or {}
            if not isinstance(data, dict):
                data = {}
            ref = str(data.get("fileName") or "").strip()
            if not ref:
                return {"ok": False, "error": "runninghub upload ok but missing fileName"}
            return {"ok": True, "provider": "runninghub", "ref": ref, "raw": data}

        base_url = str(getattr(ctx.settings, "comfyui_base_url", "") or "").strip().rstrip("/")
        if not base_url:
            return {"ok": False, "error": "missing COMFYUI_BASE_URL"}

        data = {"type": str(type or "input").strip() or "input", "overwrite": "true" if str(overwrite).strip().lower() in ("1", "true", "yes", "y", "on") else "false"}
        sf = str(subfolder or "").strip()
        if sf:
            data["subfolder"] = sf

        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(f"{base_url}/upload/image", data=data, files={"image": (fname, f0, ctype)})
        r.raise_for_status()
        obj = r.json()
        if not isinstance(obj, dict):
            return {"ok": False, "error": "comfyui response invalid"}
        name = str(obj.get("name") or fname).strip()
        sub = str(obj.get("subfolder") or "").strip()
        ref = f"{sub}/{name}" if sub else name
        return {"ok": True, "provider": "comfyui", "ref": ref, "raw": obj}

    register_admin(app, ctx)

    return app


async def handle_callback_payload(ctx: AppContext, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"ok": True}

    prompt_id = payload.get("prompt_id") or payload.get("promptId")
    prompt_id = str(prompt_id) if isinstance(prompt_id, str) and prompt_id else None

    cb_ctx = _extract_callback_context(payload)
    record_id = cb_ctx.get("record_id") or cb_ctx.get("recordId")
    record_id = str(record_id) if record_id else None

    write_back = cb_ctx.get("writeBack")
    write_back_enabled = not (isinstance(write_back, bool) and not write_back)

    table_key = _resolve_table_key(ctx, cb_ctx)
    chat_id = cb_ctx.get("chat_id")
    user_open_id = cb_ctx.get("user_open_id") or cb_ctx.get("userOpenId")
    workflow_name = cb_ctx.get("workflow")
    run_log_table_key = cb_ctx.get("runLogTableKey") or cb_ctx.get("run_log_table_key")
    run_log_record_id = cb_ctx.get("runLogRecordId") or cb_ctx.get("run_log_record_id")
    run_log_submitted_at_ms = cb_ctx.get("runLogSubmittedAtMs") or cb_ctx.get("run_log_submitted_at_ms")

    if (not record_id or not table_key) and prompt_id and hasattr(ctx.runner, "resolve_prompt"):
        try:
            resolved = await ctx.runner.resolve_prompt(prompt_id=prompt_id)
        except Exception:
            resolved = None
        if isinstance(resolved, dict):
            if not record_id:
                rid = resolved.get("record_id")
                record_id = str(rid) if rid else record_id
            if not table_key:
                tk = resolved.get("table_key")
                table_key = str(tk) if tk else table_key
            if not workflow_name:
                wk = resolved.get("workflow_key")
                workflow_name = str(wk) if wk else workflow_name
            if not chat_id:
                cid = resolved.get("chat_id")
                chat_id = str(cid) if cid else chat_id
            if not user_open_id:
                uoid = resolved.get("user_open_id")
                user_open_id = str(uoid) if uoid else user_open_id
            if not run_log_table_key:
                tlk = resolved.get("run_log_table_key")
                run_log_table_key = str(tlk) if tlk else run_log_table_key
            if not run_log_record_id:
                rlr = resolved.get("run_log_record_id")
                run_log_record_id = str(rlr) if rlr else run_log_record_id
            if run_log_submitted_at_ms is None:
                run_log_submitted_at_ms = resolved.get("run_log_submitted_at_ms")
            if isinstance(resolved.get("split_group"), str) and not cb_ctx.get("split_group"):
                cb_ctx["split_group"] = resolved.get("split_group")
            if resolved.get("split_total") is not None and cb_ctx.get("split_total") is None:
                cb_ctx["split_total"] = resolved.get("split_total")
            if resolved.get("split_index") is not None and cb_ctx.get("split_index") is None:
                cb_ctx["split_index"] = resolved.get("split_index")
            if resolved.get("append_output") is not None and cb_ctx.get("append_output") is None:
                cb_ctx["append_output"] = resolved.get("append_output")

    if (not record_id or not table_key) and prompt_id:
        rid, tk = await _resolve_record_by_prompt_id(ctx, prompt_id=prompt_id)
        if not record_id and rid:
            record_id = rid
        if not table_key and tk:
            table_key = tk

    bitable = ctx.bitables.get(table_key) if table_key else None
    table_cfg = ctx.bitable_configs.get(table_key) if table_key else None
    runlog_bitable = ctx.bitables.get(str(run_log_table_key)) if isinstance(run_log_table_key, str) and run_log_table_key else None
    runlog_cfg = ctx.bitable_configs.get(str(run_log_table_key)) if isinstance(run_log_table_key, str) and run_log_table_key else None

    dump_dir = os.environ.get("CALLBACK_DUMP_DIR", "").strip()
    if dump_dir:
        try:
            os.makedirs(dump_dir, exist_ok=True)
            name = _safe_filename(prompt_id or "callback")
            Path(dump_dir, f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            Path(dump_dir, f"{name}.context.json").write_text(
                json.dumps(
                    {"prompt_id": prompt_id, "record_id": record_id, "table_key": table_key, "workflow": workflow_name, "cb_ctx": cb_ctx},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    workflow_cfg = None
    if isinstance(workflow_name, str):
        raw = (ctx.config.get("workflows") or {}).get(workflow_name)
        if isinstance(raw, dict):
            workflow_cfg = raw

    base_override = None
    if isinstance(workflow_cfg, dict):
        ob = workflow_cfg.get("comfyuiBaseUrl") or workflow_cfg.get("comfyui_base_url") or workflow_cfg.get("comfyui_base")
        if isinstance(ob, str) and ob.strip():
            base_override = ob.strip()

    base0 = cb_ctx.get("comfyui_base_url")
    base0 = base0.strip() if isinstance(base0, str) else ""
    if not base0:
        cb_ctx["comfyui_base_url"] = base_override or ctx.settings.comfyui_base_url

    view_base2 = cb_ctx.get("comfyui_base_url")
    view_base2 = view_base2 if isinstance(view_base2, str) else ""
    result_urls = _collect_result_urls(payload, base_url=view_base2)

    status = payload.get("status")
    status = str(status).strip().lower() if isinstance(status, str) else ""
    completed = payload.get("completed")
    completed = bool(completed) if isinstance(completed, bool) else None

    ok: bool
    err: str | None
    if status in ("success", "succeeded", "ok"):
        ok = True
        err = None
    elif status in ("error", "failed", "failure"):
        ok = False
        err = _extract_error(payload) or "failed"
    else:
        err = _extract_error(payload)
        if err:
            ok = False
        else:
            ok = True if completed is None else bool(completed)

    is_final = bool(completed is True or status in ("success", "succeeded", "ok", "error", "failed", "failure"))
    if is_final and prompt_id:
        now = time.time()
        async with _DONE_NOTIFIED_LOCK:
            for k, ts in list(_DONE_NOTIFIED.items()):
                if not k or (now - float(ts or 0.0)) > 3600:
                    _DONE_NOTIFIED.pop(k, None)
            if prompt_id in _DONE_NOTIFIED:
                return {"ok": True}
            _DONE_NOTIFIED[prompt_id] = now

    split_summary: dict[str, Any] | None = None

    provider = payload.get("provider")
    provider = str(provider).strip().lower() if isinstance(provider, str) else ""
    if provider not in ("runninghub",) and prompt_id and (not isinstance(payload.get("result"), dict) or not _iter_output_items(payload)):
        base = _strip_ticks(str(cb_ctx.get("comfyui_base_url") or "")).strip() or ctx.settings.comfyui_base_url
        cli = ctx.comfyui if base.rstrip("/") == ctx.settings.comfyui_base_url.rstrip("/") else ComfyUIClient(base)
        try:
            item = await cli.get_history_item(prompt_id=prompt_id)
        except Exception:
            item = None
        if isinstance(item, dict):
            payload["result"] = item
            if not result_urls:
                result_urls = _collect_result_urls(payload, base_url=view_base2)

    file_paths: list[str] = []
    file_tokens: list[dict[str, Any]] = []
    writeback_errors: list[str] = []
    runlog_file_tokens: list[dict[str, Any]] = []

    need_send_media = bool(isinstance(chat_id, str) and chat_id and not record_id)
    if (write_back_enabled and bitable and table_cfg and bitable.mode.write_enabled and ctx.drive) or need_send_media:
        output_items = _iter_output_items(payload)
        view_base = cb_ctx.get("comfyui_base_url")
        if not isinstance(view_base, str):
            view_base = ""
        for it in output_items:
            url = it.get("fileUrl") or it.get("fileURL") or it.get("file_url") or it.get("url")
            if isinstance(url, str) and _is_http_url(url):
                try:
                    saved = await _download_from_url(ctx, url=url, prompt_id=prompt_id, filename=it.get("filename") if isinstance(it.get("filename"), str) else None)
                except Exception:
                    saved = None
                if saved and _is_file_path(saved):
                    file_paths.append(saved)
                    continue
            fullpath = it.get("fullpath")
            if isinstance(fullpath, str) and _is_file_path(fullpath):
                file_paths.append(fullpath)
                continue
            filename = it.get("filename")
            if not isinstance(filename, str) or not filename:
                continue
            subfolder = it.get("subfolder") if isinstance(it.get("subfolder"), str) else ""
            type_ = it.get("type") if isinstance(it.get("type"), str) else "output"
            try:
                saved = await _download_from_view(
                    ctx,
                    base_url=view_base,
                    filename=filename,
                    subfolder=subfolder,
                    type_=type_,
                    prompt_id=prompt_id,
                )
            except Exception:
                saved = None
            if saved and _is_file_path(saved):
                file_paths.append(saved)

        if not file_paths:
            url_items = _iter_url_items(payload)
            for it in url_items:
                url = it.get("fileUrl") or it.get("fileURL") or it.get("file_url") or it.get("url")
                if not isinstance(url, str) or not _is_http_url(url):
                    continue
                name = it.get("filename") or it.get("name")
                fname = str(name) if isinstance(name, str) and name else None
                try:
                    saved = await _download_from_url(ctx, url=url, prompt_id=prompt_id, filename=fname)
                except Exception:
                    saved = None
                if saved and _is_file_path(saved):
                    file_paths.append(saved)

    if not file_paths:
        candidates = _extract_output_candidates(payload)
        file_paths = [c for c in candidates if _is_file_path(c)]

    if file_paths:
        seen: set[str] = set()
        uniq: list[str] = []
        for fp in file_paths:
            try:
                ap = os.path.abspath(fp)
            except Exception:
                ap = fp
            if ap in seen:
                continue
            seen.add(ap)
            uniq.append(fp)
        file_paths = uniq

    if (
        write_back_enabled
        and record_id
        and file_paths
        and bitable
        and table_cfg
        and bitable.mode.write_enabled
        and ctx.drive
    ):
        for fp in file_paths:
            try:
                up = await ctx.drive.upload_to_bitable(
                    app_token=table_cfg.app_token,
                    file_path=fp,
                    as_image=_guess_is_image(fp),
                )
                file_tokens.append({"file_token": up.file_token})
            except Exception as e:
                msg = f"Failed to upload {fp}: {e}"
                writeback_errors.append(msg)
                logging.warning(msg)

    if (
        write_back_enabled
        and run_log_record_id
        and runlog_bitable
        and runlog_cfg
        and getattr(runlog_bitable, "mode", None)
        and getattr(runlog_bitable.mode, "write_enabled", False)
        and ctx.drive
        and file_paths
    ):
        for fp in file_paths:
            try:
                up = await ctx.drive.upload_to_bitable(
                    app_token=runlog_cfg.app_token,
                    file_path=fp,
                    as_image=_guess_is_image(fp),
                )
                runlog_file_tokens.append({"file_token": up.file_token})
            except Exception as e:
                msg = f"Failed to upload(runlog) {fp}: {e}"
                writeback_errors.append(msg)
                logging.warning(msg)

    if write_back_enabled and record_id and bitable and table_cfg and bitable.mode.write_enabled:
        try:
            await _write_back_record(
                bitable=bitable,
                table_cfg=table_cfg,
                workflow_cfg=workflow_cfg,
                record_id=record_id,
                ok=ok,
                file_tokens=file_tokens,
                result_urls=result_urls,
                prompt_id=prompt_id,
                error=err,
                cb_ctx=cb_ctx if isinstance(cb_ctx, dict) else None,
            )
        except Exception as e:
            msg = f"write back record failed: table={table_key} record={record_id} err={e}"
            writeback_errors.append(msg)
            logging.warning(msg)

    if write_back_enabled and run_log_record_id and runlog_bitable and runlog_cfg and getattr(runlog_bitable, "mode", None) and getattr(runlog_bitable.mode, "write_enabled", False):
        try:
            payload_for_runlog = dict(payload) if isinstance(payload, dict) else {}
            if runlog_file_tokens:
                payload_for_runlog["runlog_output_file_tokens"] = runlog_file_tokens
            await _enc_update_runlog_record(
                runlog_bitable=runlog_bitable,
                runlog_cfg=runlog_cfg,
                run_log_record_id=str(run_log_record_id),
                run_log_submitted_at_ms=run_log_submitted_at_ms,
                ok=ok,
                payload=payload_for_runlog,
                err=err,
            )
        except Exception as e:
            msg = f"write runlog failed: table={run_log_table_key} record={run_log_record_id} err={e}"
            writeback_errors.append(msg)
            logging.warning(msg)

    if isinstance(cb_ctx, dict) and cb_ctx.get("split_group") and record_id and table_key:
        try:
            split_summary = await _update_split_progress_and_maybe_finalize(
                bitable=bitable,
                table_cfg=table_cfg,
                workflow_cfg=workflow_cfg,
                table_key=str(table_key or ""),
                record_id=str(record_id or ""),
                ok=ok,
                cb_ctx=cb_ctx,
            )
        except Exception:
            split_summary = None

    if writeback_errors and not (isinstance(cb_ctx, dict) and cb_ctx.get("split_group")):
        try:
            tip = writeback_errors[0]
            extra = f"（另有 {len(writeback_errors) - 1} 个回写/上传错误）" if len(writeback_errors) > 1 else ""
            im0 = IMClient(ctx.auth)
            if isinstance(chat_id, str) and chat_id:
                await im0.send_text(chat_id=chat_id, text=f"飞书回写/上传失败{extra}：{tip}")
            elif isinstance(user_open_id, str) and user_open_id:
                await im0.send_text_to_open_id(open_id=user_open_id, text=f"飞书回写/上传失败{extra}：{tip}")
        except Exception:
            pass

    if prompt_id:
        try:
            await ctx.runner.on_done(prompt_id=prompt_id)
        except Exception:
            pass

    target_chat_id = str(chat_id or "").strip()
    target_open_id = str(user_open_id or "").strip()

    async def _notify_text(im: IMClient, text: str) -> None:
        if target_chat_id:
            await im.send_text(chat_id=target_chat_id, text=text)
            return
        if target_open_id:
            await im.send_text_to_open_id(open_id=target_open_id, text=text)

    if target_chat_id or target_open_id:
        im = IMClient(ctx.auth)
        rid0 = str(record_id or "").strip()
        split_group = str((cb_ctx or {}).get("split_group") or "").strip() if isinstance(cb_ctx, dict) else ""
        if rid0 and not rid0.startswith("mock_rec_"):
            if split_group:
                idx0 = (cb_ctx or {}).get("split_index") if isinstance(cb_ctx, dict) else None
                idx = int(idx0) if isinstance(idx0, int) else (int(str(idx0)) if str(idx0).strip().isdigit() else 0)
                total0 = (cb_ctx or {}).get("split_total") if isinstance(cb_ctx, dict) else None
                total = int(total0) if isinstance(total0, int) else (int(str(total0)) if str(total0).strip().isdigit() else int((split_summary or {}).get("total") or 0))
                done = int((split_summary or {}).get("done") or 0) if isinstance(split_summary, dict) else 0
                failed = int((split_summary or {}).get("failed") or 0) if isinstance(split_summary, dict) else 0
                succ = max(0, done - failed)
                pid_short = (prompt_id or "").strip() if isinstance(prompt_id, str) else ""
                msg_lines: list[str] = []
                msg_lines.append(f"子任务 {idx + 1}/{total} {'成功' if ok else '失败'}（进度 {done}/{total}，成功{succ}，失败{failed}）")
                if rid0:
                    msg_lines.append(f"record={rid0}")
                if pid_short:
                    msg_lines.append(f"task={pid_short}")
                if not ok and isinstance(err, str) and err.strip():
                    first_err = err.strip().splitlines()[0]
                    if len(first_err) > 200:
                        first_err = first_err[:200] + "..."
                    msg_lines.append(f"原因：{first_err}")
                if ok:
                    out_n = len(runlog_file_tokens) if runlog_file_tokens else (len(file_paths) if file_paths else len(result_urls))
                    if out_n:
                        msg_lines.append(f"产出：{out_n}")
                if writeback_errors:
                    tip = str(writeback_errors[0] or "").strip()
                    if tip:
                        if len(tip) > 200:
                            tip = tip[:200] + "..."
                        msg_lines.append(f"回写提示：{tip}")
                await _notify_text(im, "\n".join([x for x in msg_lines if x]))

                if isinstance(split_summary, dict) and bool(split_summary.get("final")):
                    total2 = int(split_summary.get("total") or 0)
                    done2 = int(split_summary.get("done") or 0)
                    failed2 = int(split_summary.get("failed") or 0)
                    succ2 = max(0, done2 - failed2)
                    await _notify_text(im, f"批次完成 record={record_id}：成功{succ2}，失败{failed2}，共{total2}")
            else:
                await _notify_text(im, ("已完成" if ok else "失败") + f" record={record_id}")
        elif file_paths:
            sent_any = False
            last_send_err: str | None = None

            async def _send_one_file(fp: str) -> bool:
                nonlocal last_send_err
                ext = Path(fp).suffix.lower().lstrip(".")
                size = os.path.getsize(fp) if fp and os.path.exists(fp) else 0
                if size <= 0:
                    raise RuntimeError("file not found or empty")
                if not target_chat_id:
                    return False
                if ext == "mp4" and size <= 30 * 1024 * 1024:
                    try:
                        k = await im.upload_video_message(file_path=fp, duration_ms=None)
                        await im.send_media(chat_id=target_chat_id, file_key=k)
                        return True
                    except Exception as e:
                        last_send_err = str(e)
                if ext in {"png", "jpg", "jpeg", "webp", "gif", "bmp"} and size <= 10 * 1024 * 1024:
                    try:
                        k = await im.upload_image_message(file_path=fp)
                        await im.send_image(chat_id=target_chat_id, image_key=k)
                        return True
                    except Exception as e:
                        last_send_err = str(e)
                if size <= 30 * 1024 * 1024:
                    k = await im.upload_file_message(file_path=fp)
                    await im.send_file(chat_id=target_chat_id, file_key=k)
                    return True
                raise RuntimeError("file too large to upload via im api")

            for fp in file_paths:
                try:
                    if await _send_one_file(fp):
                        sent_any = True
                except Exception as e:
                    last_send_err = str(e)

            if not sent_any:
                if result_urls:
                    lines = list(result_urls)
                    if last_send_err:
                        lines.append(f"（补充：发送预览失败，原因：{last_send_err}）")
                    await _notify_text(im, "\n".join(lines))
                else:
                    text0 = ("已完成" if ok else "失败") + (f" prompt={prompt_id}" if prompt_id else "")
                    if last_send_err:
                        text0 = text0 + f"\n（补充：发送预览失败，原因：{last_send_err}）"
                    await _notify_text(im, text0)
        elif result_urls:
            lines = [("已完成" if ok else "失败") + (f" prompt={prompt_id}" if prompt_id else "")]
            lines.extend(result_urls)
            await _notify_text(im, "\n".join(lines))
        elif prompt_id:
            await _notify_text(im, ("已完成" if ok else "失败") + f" prompt={prompt_id}")

    for fp in file_paths:
        try:
            if fp and os.path.exists(fp) and ctx.settings.bitable_download_dir in os.path.abspath(fp):
                os.remove(fp)
        except Exception:
            pass

    return {"ok": True}
