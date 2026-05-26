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

_DONE_NOTIFIED: dict[str, float] = {}
_DONE_NOTIFIED_LOCK = asyncio.Lock()


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
    error: str | None,
) -> None:
    fields_cfg = dict(getattr(table_cfg, "fields", {}) or {})
    status_values_cfg = dict(getattr(table_cfg, "status_values", {}) or {})
    if isinstance(workflow_cfg, dict):
        wf_fields = workflow_cfg.get("writeBackFields")
        if isinstance(wf_fields, dict):
            for k, v in wf_fields.items():
                if isinstance(k, str) and isinstance(v, str):
                    fields_cfg[k] = v
        wf_status_values = workflow_cfg.get("writeBackStatusValues")
        if isinstance(wf_status_values, dict):
            for k, v in wf_status_values.items():
                if isinstance(k, str) and isinstance(v, str):
                    status_values_cfg[k] = v

    fields: dict[str, Any] = {}
    status_field = fields_cfg.get("status")
    if status_field:
        status_key = "done" if ok else "failed"
        status_value = status_values_cfg.get(status_key)
        if status_value:
            fields[status_field] = status_value

    output_field = fields_cfg.get("output")
    if output_field and file_tokens:
        fields[output_field] = file_tokens

    error_field = fields_cfg.get("error")
    if error_field:
        fields[error_field] = "" if ok else (error or "unknown error")

    if fields:
        try:
            await bitable.update_record(record_id, fields)
        except Exception as e:
            msg = str(e)
            m = re.search(r"fields\.([^'\"\s]+)", msg)
            if m:
                missing = m.group(1)
                fields2 = {k: v for k, v in fields.items() if str(k) != missing}
                if fields2 and fields2 != fields:
                    await bitable.update_record(record_id, fields2)
            else:
                raise


def create_callback_app(ctx: AppContext) -> FastAPI:
    app = FastAPI()

    @app.post(ctx.settings.callback_path)
    async def callback(req: Request) -> dict[str, Any]:
        payload = await req.json()
        return await handle_callback_payload(ctx, payload)

    @app.post("/_local/exec")
    async def local_exec(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        from .commands import parse_message_text
        from .dispatcher import TriggerContext, build_panel_card, run_workflow, _reset_table_records

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
            return {"ok": True, "card": build_panel_card()}

        if name in ("help", "h"):
            return {"ok": True, "help": "Check IM for help message."}

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

    @app.get("/_local/bitable/fields")
    async def local_bitable_fields(table: str | None = None) -> dict[str, Any]:
        table_key = str(table or ctx.default_table_key or "")
        bitable = ctx.bitables.get(table_key) if table_key else None
        if not bitable:
            return {"ok": False, "error": "missing table"}
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
            return {"ok": False, "error": "missing table"}
        if not record_id:
            return {"ok": False, "error": "missing record_id"}
        try:
            rec = await bitable.get_record(str(record_id))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "table": table_key, "record": rec}

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
    workflow_name = cb_ctx.get("workflow")

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

    if (not record_id or not table_key) and prompt_id:
        rid, tk = await _resolve_record_by_prompt_id(ctx, prompt_id=prompt_id)
        if not record_id and rid:
            record_id = rid
        if not table_key and tk:
            table_key = tk

    bitable = ctx.bitables.get(table_key) if table_key else None
    table_cfg = ctx.bitable_configs.get(table_key) if table_key else None

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
                print(f"Failed to upload {fp}: {e}")

    if write_back_enabled and record_id and bitable and table_cfg and bitable.mode.write_enabled:
        try:
            await _write_back_record(
                bitable=bitable,
                table_cfg=table_cfg,
                workflow_cfg=workflow_cfg,
                record_id=record_id,
                ok=ok,
                file_tokens=file_tokens,
                error=err,
            )
        except Exception:
            pass

    if prompt_id:
        try:
            await ctx.runner.on_done(prompt_id=prompt_id)
        except Exception:
            pass

    if isinstance(chat_id, str) and chat_id:
        im = IMClient(ctx.auth)
        rid0 = str(record_id or "").strip()
        if rid0 and not rid0.startswith("mock_rec_"):
            await im.send_text(chat_id=chat_id, text=("已完成" if ok else "失败") + f" record={record_id}")
        elif file_paths:
            sent_any = False
            last_send_err: str | None = None

            async def _send_one_file(fp: str) -> bool:
                nonlocal last_send_err
                ext = Path(fp).suffix.lower().lstrip(".")
                size = os.path.getsize(fp) if fp and os.path.exists(fp) else 0
                if size <= 0:
                    raise RuntimeError("file not found or empty")
                if ext == "mp4" and size <= 30 * 1024 * 1024:
                    try:
                        k = await im.upload_video_message(file_path=fp, duration_ms=None)
                        await im.send_media(chat_id=chat_id, file_key=k)
                        return True
                    except Exception as e:
                        last_send_err = str(e)
                if ext in {"png", "jpg", "jpeg", "webp", "gif", "bmp"} and size <= 10 * 1024 * 1024:
                    try:
                        k = await im.upload_image_message(file_path=fp)
                        await im.send_image(chat_id=chat_id, image_key=k)
                        return True
                    except Exception as e:
                        last_send_err = str(e)
                if size <= 30 * 1024 * 1024:
                    k = await im.upload_file_message(file_path=fp)
                    await im.send_file(chat_id=chat_id, file_key=k)
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
                    await im.send_text(chat_id=chat_id, text="\n".join(lines))
                else:
                    text0 = ("已完成" if ok else "失败") + (f" prompt={prompt_id}" if prompt_id else "")
                    if last_send_err:
                        text0 = text0 + f"\n（补充：发送预览失败，原因：{last_send_err}）"
                    await im.send_text(chat_id=chat_id, text=text0)
        elif result_urls:
            lines = [("已完成" if ok else "失败") + (f" prompt={prompt_id}" if prompt_id else "")]
            lines.extend(result_urls)
            await im.send_text(chat_id=chat_id, text="\n".join(lines))
        elif prompt_id:
            await im.send_text(chat_id=chat_id, text=("已完成" if ok else "失败") + f" prompt={prompt_id}")

    for fp in file_paths:
        try:
            if fp and os.path.exists(fp) and ctx.settings.bitable_download_dir in os.path.abspath(fp):
                os.remove(fp)
        except Exception:
            pass

    return {"ok": True}
