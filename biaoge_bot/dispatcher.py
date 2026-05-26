from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .callback_server import handle_callback_payload
from .comfyui import ComfyUIClient
from .context import AppContext
from .im import IMClient
from .license_guard import check_license
from .runninghub import RunningHubClient
from .workflows import WorkflowSpec


@dataclass(frozen=True)
class TriggerContext:
    chat_id: str | None
    user_open_id: str | None
    source: str


def build_panel_card() -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "ComfyUI 控制面板"}},
        "elements": [
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "运行默认流程"},
                        "type": "primary",
                        "value": {"cmd": "run_default"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "执行队列(drain)"},
                        "type": "danger",
                        "value": {"cmd": "drain"},
                    },
                ],
            }
        ],
    }


def _pick_record_id(args: dict[str, Any]) -> str | None:
    for k in ("record", "record_id", "recordId"):
        v = args.get(k)
        if v:
            return str(v)
    return None


def _pick_row(args: dict[str, Any]) -> int | None:
    for k in ("row", "row_no", "rowNo", "line"):
        v = args.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        if s.isdigit():
            n = int(s)
            return n if n > 0 else None
    return None


def _pick_view_id(args: dict[str, Any]) -> str | None:
    for k in ("view", "view_id", "viewId"):
        v = args.get(k)
        if v:
            s = str(v).strip()
            return s if s else None
    return None


def _pick_table_key(args: dict[str, Any]) -> str | None:
    for k in ("table", "tableKey", "table_key"):
        v = args.get(k)
        if v:
            return str(v)
    return None


def _pick_workflow_key(args: dict[str, Any]) -> str | None:
    for k in ("workflow", "workflowName", "wf", "name"):
        v = args.get(k)
        if v:
            return str(v)
    return None


def _args_without_meta(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (args or {}).items():
        if k in ("record", "record_id", "recordId", "row", "row_no", "rowNo", "line", "view", "view_id", "viewId", "workflow", "workflowName", "wf", "name", "table", "tableKey", "table_key"):
            continue
        out[k] = v
    return out


def _is_local_base_url(url: str) -> bool:
    u = (url or "").strip()
    if not u:
        return True
    try:
        p = urlparse(u)
    except Exception:
        return False
    host = (p.hostname or "").strip().lower()
    if host in ("127.0.0.1", "localhost"):
        return True
    return False


def _strip_ticks(s: str) -> str:
    v = (s or "").strip()
    if v.startswith("`") and v.endswith("`") and len(v) >= 2:
        v = v[1:-1].strip()
    return v


def _pick_callback_url(ctx: AppContext) -> str:
    mode = str(getattr(ctx.settings, "remote_result_mode", "") or "").strip().lower()
    if mode == "poll":
        if _is_local_base_url(ctx.settings.comfyui_base_url):
            return ctx.settings.callback_url
        return ""
    if ctx.settings.remote_callback_url and not _is_local_base_url(ctx.settings.comfyui_base_url):
        return _strip_ticks(str(ctx.settings.remote_callback_url).strip())
    return ctx.settings.callback_url


def _pick_callback_url_for_base(ctx: AppContext, base_url: str) -> str:
    mode = str(getattr(ctx.settings, "remote_result_mode", "") or "").strip().lower()
    if mode == "poll":
        if _is_local_base_url(base_url):
            return ctx.settings.callback_url
        return ""
    if ctx.settings.remote_callback_url and not _is_local_base_url(base_url):
        return _strip_ticks(str(ctx.settings.remote_callback_url).strip())
    return ctx.settings.callback_url


def _pick_default_workflow_key(ctx: AppContext, *, table_key: str | None) -> str | None:
    dw = getattr(ctx, "default_workflow_key", None)
    if isinstance(dw, str) and dw and ctx.workflows.get(dw):
        cfg = (ctx.config.get("workflows") or {}).get(dw) or {}
        if not table_key or (isinstance(cfg, dict) and cfg.get("table") == table_key):
            return dw
    if ctx.workflows.get("default"):
        return "default"
    if table_key:
        for k in ctx.workflows._specs.keys():
            wf_cfg = (ctx.config.get("workflows") or {}).get(k) or {}
            if isinstance(wf_cfg, dict) and wf_cfg.get("table") == table_key:
                return k
    first = ctx.workflows.first()
    return first.key if first else None


def _pick_table_key_for_workflow(ctx: AppContext, *, args: dict[str, Any], workflow_key: str | None) -> str | None:
    t = _pick_table_key(args)
    if t:
        return t
    wk = str(workflow_key or "").strip()
    if wk:
        wf_cfg = (ctx.config.get("workflows") or {}).get(wk) or {}
        if isinstance(wf_cfg, dict):
            v = wf_cfg.get("table")
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ctx.default_table_key


def _b64url_json_decode(data: str) -> dict[str, Any] | None:
    s = str(data or "").strip()
    if not s:
        return None
    pad = (-len(s)) % 4
    if pad:
        s = s + ("=" * pad)
    try:
        raw = base64.urlsafe_b64decode(s.encode("utf-8"))
    except Exception:
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


async def handle_help(im: IMClient, chat_id: str) -> None:
    await im.send_text(
        chat_id=chat_id,
        text=(
            "指令说明：\n"
            "/panel  —— 打开控制面板\n"
            "/run_default  —— 运行默认工作流\n"
            "/run record=recxxxx seed=1 steps=30 prompt=...  —— 指定记录 ID 运行默认工作流，并支持参数覆盖\n"
            "/run row=6 seed=1 steps=30 prompt=...  —— 指定行号运行默认工作流，并支持参数覆盖\n"
            "/wf <workflow> record=recxxxx seed=1 steps=30 prompt=...  —— 指定工作流和记录 ID 运行\n"
            "/wf <workflow> row=6 seed=1 steps=30 prompt=...  —— 指定工作流和行号运行\n"
            "/wf <workflow> row=6 view=vewxxxx  —— 指定工作流、视图及行号运行\n"
            "/wf <workflow> 3.seed=1 10.text=hello  —— 指定工作流运行并直接覆盖节点参数\n"
            "/wf <workflow> images=@E:\\\\pics\\\\a.jpg  —— 本机文件上传后再执行（支持用引号包住带空格的路径）\n"
            "/batch <workflow> table=face_table batch=10 inflight=1  —— 批量运行指定数量的任务\n"
            "/drain <workflow> table=face_table batch=10 inflight=1  —— 持续处理队列直到耗尽\n"
            "/stop_queue <workflow> table=face_table  —— 停止当前的批量/队列任务\n"
        ),
    )


async def _send_license_guidance(im: IMClient, chat_id: str) -> None:
    lic = check_license()
    await im.send_text(
        chat_id=chat_id,
        text=(
            "未检测到有效授权，已禁用 Bitable/Drive。\n"
            f"设备码: {lic.device_code}\n"
            f"请将 license.lic 放置到: {str(lic.license_path)}\n"
            "将设备码发给授权方生成 license.lic，放置后重启。"
        ),
    )


async def queue_by_workflowprompt(
    comfyui: ComfyUIClient,
    *,
    wf: WorkflowSpec,
    node_info_list: list[dict[str, Any]],
    extra_data: dict[str, Any],
) -> str | None:
    res = await comfyui.queue_workflow(
        workflow_name=wf.workflow_name,
        node_info_list=node_info_list,
        extra_data=extra_data,
    )
    return res.prompt_id


def _extract_record_fields(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields")
    if isinstance(fields, dict):
        return fields
    return {}


def _extract_attachment_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    return []

def _collect_file_paths(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: list[str] = []
        for x in value:
            out.extend(_collect_file_paths(x))
        return out
    if isinstance(value, dict):
        out2: list[str] = []
        for x in value.values():
            out2.extend(_collect_file_paths(x))
        return out2
    return [str(value)]


def _runninghub_node_file_value(file_name: str) -> str:
    s = str(file_name or "").strip()
    if not s:
        return ""
    return s.lstrip("/")


async def _download_attachments(
    ctx: AppContext,
    *,
    provider: str,
    comfyui: ComfyUIClient,
    runninghub: RunningHubClient | None,
    value: Any,
) -> list[str]:
    items = _extract_attachment_items(value)
    if not items:
        return []
    if not ctx.drive:
        return []
    out: list[str] = []
    for it in items:
        token = it.get("file_token") or it.get("fileToken")
        if not token:
            continue
        file_name = it.get("name") or it.get("file_name") or it.get("fileName")
        download_dir = ctx.settings.bitable_download_dir if provider == "runninghub" else (ctx.settings.bitable_download_dir if ctx.settings.comfyui_upload_enabled else (ctx.settings.comfyui_input_dir or ctx.settings.bitable_download_dir))
        saved = await ctx.drive.download_media(file_token=str(token), download_dir=download_dir, file_name=str(file_name) if file_name else None)
        if provider == "runninghub":
            if not runninghub:
                continue
            uploaded = await runninghub.upload_media_binary(file_path=saved)
            fv = _runninghub_node_file_value(uploaded.file_name or "")
            if fv:
                out.append(fv)
            try:
                if os.path.exists(saved) and ctx.settings.bitable_download_dir in os.path.abspath(saved):
                    os.remove(saved)
            except Exception:
                pass
        elif ctx.settings.comfyui_upload_enabled:
            uploaded = await comfyui.upload_image(
                file_path=saved,
                filename=Path(saved).name,
                type="input",
                overwrite=ctx.settings.comfyui_upload_overwrite,
                subfolder=ctx.settings.comfyui_upload_subfolder,
            )
            name = str(uploaded.get("name") or Path(saved).name)
            sub = str(uploaded.get("subfolder") or "")
            out.append(f"{sub}/{name}" if sub else name)
            try:
                if os.path.exists(saved) and ctx.settings.bitable_download_dir in os.path.abspath(saved):
                    os.remove(saved)
            except Exception:
                pass
        elif ctx.settings.comfyui_input_dir:
            out.append(Path(saved).name)
        else:
            out.append(saved)
    return out


async def _upload_local_file_for_provider(
    *,
    provider: str,
    comfyui: ComfyUIClient,
    runninghub: RunningHubClient | None,
    file_path: str,
    overwrite: bool,
    subfolder: str | None,
) -> str:
    p = str(file_path or "").strip()
    if not p:
        raise RuntimeError("empty file_path")
    if os.name == "nt":
        if p.startswith("/") or p.startswith("\\"):
            raise RuntimeError(f"file not found: {p} (looks like a Linux path; this bot is running on Windows, please use a local Windows path like E:\\\\pics\\\\a.jpg, or remove '@' if the file already exists on the ComfyUI machine)")
    else:
        if re.match(r"^[A-Za-z]:\\\\", p):
            raise RuntimeError(f"file not found: {p} (looks like a Windows path; this bot is running on Linux, please use a local Linux path like /home/... , or remove '@' if the file already exists on the ComfyUI machine)")
    if not os.path.exists(p):
        raise RuntimeError(f"file not found: {p}")
    if not os.path.isfile(p):
        raise RuntimeError(f"not a file: {p}")

    if provider == "runninghub":
        if not runninghub:
            raise RuntimeError("missing runninghub client")
        uploaded = await runninghub.upload_media_binary(file_path=p)
        fv = _runninghub_node_file_value(uploaded.file_name or "")
        if not fv:
            raise RuntimeError("runninghub upload ok but missing fileName")
        return fv

    uploaded = await comfyui.upload_image(
        file_path=p,
        filename=Path(p).name,
        type="input",
        overwrite=overwrite,
        subfolder=subfolder,
    )
    name = str(uploaded.get("name") or Path(p).name)
    sub = str(uploaded.get("subfolder") or "")
    return f"{sub}/{name}" if sub else name


async def _resolve_file_refs_in_params(
    ctx: AppContext,
    *,
    trigger: TriggerContext,
    provider: str,
    comfyui: ComfyUIClient,
    runninghub: RunningHubClient | None,
    wf: WorkflowSpec,
    params: dict[str, Any],
) -> dict[str, Any]:
    def is_inline_node_key(k: str) -> bool:
        return bool(_INLINE_NODE_KEY.match(str(k)))

    def is_file_ref(s: str) -> bool:
        v = str(s or "").strip()
        if v.startswith("@msg:") or v.startswith("@msgid:"):
            return False
        return v.startswith("@") or (":@" in v and v.split(":@", 1)[0] in ("file", "image", "video", "audio"))

    def is_msg_ref(s: str) -> bool:
        v = str(s or "").strip().lower()
        return v.startswith("@msg:") or v.startswith("@msgid:")

    def parse_msg_ref(s: str) -> tuple[str, str]:
        v = str(s or "").strip()
        v0 = v[1:] if v.startswith("@") else v
        head, tail = (v0.split(":", 1) + [""])[:2]
        return head.strip().lower(), tail.strip()

    def strip_file_ref(s: str) -> str:
        v = str(s or "").strip()
        if v.startswith("@"):
            return v[1:].strip()
        if ":@" in v:
            head, tail = v.split(":@", 1)
            if head in ("file", "image", "video", "audio"):
                return tail.strip()
        return v

    def _collect_values_by_key(obj: Any, keys: set[str], limit: int = 10) -> list[Any]:
        out2: list[Any] = []

        def walk(x: Any) -> None:
            if len(out2) >= limit:
                return
            if isinstance(x, dict):
                for kk, vv in x.items():
                    if isinstance(kk, str) and kk in keys and len(out2) < limit:
                        out2.append(vv)
                    walk(vv)
                return
            if isinstance(x, list):
                for it2 in x:
                    walk(it2)
                return

        walk(obj)
        return out2

    async def _pick_msg_attachment(selector: str) -> dict[str, Any] | None:
        sel0 = str(selector or "").strip().lower() or "last"
        info0 = ctx.runner.get_im_attachment(chat_id=trigger.chat_id, user_open_id=trigger.user_open_id, selector=sel0)
        if info0:
            return info0
        if not ctx.im:
            return None
        cid = str(trigger.chat_id or "").strip()
        if not cid:
            return None
        nth = 1
        if sel0.startswith("last:"):
            try:
                nth = int(sel0.split(":", 1)[1])
            except Exception:
                nth = 1
        nth = max(1, nth)
        try:
            items = await ctx.im.list_chat_messages(chat_id=cid, page_size=20)
        except Exception:
            return None
        found: list[dict[str, Any]] = []
        for it0 in items:
            body = it0.get("body") if isinstance(it0.get("body"), dict) else {}
            content_raw = body.get("content") if isinstance(body.get("content"), str) else it0.get("content")
            if not isinstance(content_raw, str) or not content_raw:
                continue
            try:
                obj = json.loads(content_raw)
            except Exception:
                continue
            img_vals = _collect_values_by_key(obj, {"image_key", "imageKey"}, limit=5)
            for v0 in img_vals:
                if isinstance(v0, str) and v0.strip():
                    found.append({"kind": "image", "key": v0.strip()})
            file_vals = _collect_values_by_key(obj, {"file_key", "fileKey"}, limit=5)
            for v1 in file_vals:
                if isinstance(v1, str) and v1.strip():
                    found.append({"kind": "file", "key": v1.strip()})
            if len(found) >= nth:
                break
        if not found or nth > len(found):
            return None
        return found[nth - 1]

    out: dict[str, Any] = dict(params or {})
    for k, v in list(out.items()):
        if not isinstance(v, str):
            continue
        s0 = v.strip()
        if not is_file_ref(s0) and not is_msg_ref(s0):
            continue

        if is_inline_node_key(str(k)):
            if "," in s0:
                raise RuntimeError(f"inline node param does not support multiple files: {k}={v}")
            if is_msg_ref(s0):
                kind, sel = parse_msg_ref(s0)
                if kind != "msg":
                    raise RuntimeError(f"unsupported msg ref: {v}")
                info = await _pick_msg_attachment(sel or "last")
                if not info:
                    raise RuntimeError("no recent attachment found for @msg:last (please send an image/file in this chat first; if the bot cannot receive non-@ messages in group chats, add @bot when sending the attachment, or use /api/upload; if the bot cannot see the attachment event, grant it message read permission so it can fetch recent messages)")
                if not ctx.im:
                    raise RuntimeError("missing im client")
                akey = str(info.get("key") or "").strip()
                akind = str(info.get("kind") or "").strip().lower()
                fname = str(info.get("file_name") or "").strip()
                ext = Path(fname).suffix if fname else (".png" if akind == "image" else ".bin")
                base_dir = str(ctx.settings.bitable_download_dir or "").strip() or os.getcwd()
                tmp_dir = os.path.join(base_dir, "_im_attachments")
                os.makedirs(tmp_dir, exist_ok=True)
                tmp_path = os.path.join(tmp_dir, f"im_{akey[:12]}{ext}")
                if akind == "image":
                    await ctx.im.download_image(image_key=akey, save_path=tmp_path)
                else:
                    await ctx.im.download_file(file_key=akey, save_path=tmp_path)
                try:
                    out[k] = await _upload_local_file_for_provider(
                        provider=provider,
                        comfyui=comfyui,
                        runninghub=runninghub,
                        file_path=tmp_path,
                        overwrite=ctx.settings.comfyui_upload_overwrite,
                        subfolder=ctx.settings.comfyui_upload_subfolder,
                    )
                finally:
                    try:
                        if os.path.exists(tmp_path) and os.path.isfile(tmp_path) and os.path.abspath(tmp_dir) in os.path.abspath(tmp_path):
                            os.remove(tmp_path)
                    except Exception:
                        pass
            else:
                p = strip_file_ref(s0)
                out[k] = await _upload_local_file_for_provider(
                    provider=provider,
                    comfyui=comfyui,
                    runninghub=runninghub,
                    file_path=p,
                    overwrite=ctx.settings.comfyui_upload_overwrite,
                    subfolder=ctx.settings.comfyui_upload_subfolder,
                )
            continue

        parts = [x.strip() for x in s0.split(",")] if "," in s0 else [s0]
        resolved_list: list[str] = []
        changed = False
        for it in parts:
            if not it:
                continue
            if is_msg_ref(it):
                kind, sel = parse_msg_ref(it)
                if kind != "msg":
                    raise RuntimeError(f"unsupported msg ref: {it}")
                info = await _pick_msg_attachment(sel or "last")
                if not info:
                    raise RuntimeError("no recent attachment found for @msg:last (please send an image/file in this chat first; if the bot cannot receive non-@ messages in group chats, add @bot when sending the attachment, or use /api/upload; if the bot cannot see the attachment event, grant it message read permission so it can fetch recent messages)")
                if not ctx.im:
                    raise RuntimeError("missing im client")
                akey = str(info.get("key") or "").strip()
                akind = str(info.get("kind") or "").strip().lower()
                fname = str(info.get("file_name") or "").strip()
                ext = Path(fname).suffix if fname else (".png" if akind == "image" else ".bin")
                base_dir = str(ctx.settings.bitable_download_dir or "").strip() or os.getcwd()
                tmp_dir = os.path.join(base_dir, "_im_attachments")
                os.makedirs(tmp_dir, exist_ok=True)
                tmp_path = os.path.join(tmp_dir, f"im_{akey[:12]}{ext}")
                if akind == "image":
                    await ctx.im.download_image(image_key=akey, save_path=tmp_path)
                else:
                    await ctx.im.download_file(file_key=akey, save_path=tmp_path)
                try:
                    resolved_list.append(
                        await _upload_local_file_for_provider(
                            provider=provider,
                            comfyui=comfyui,
                            runninghub=runninghub,
                            file_path=tmp_path,
                            overwrite=ctx.settings.comfyui_upload_overwrite,
                            subfolder=ctx.settings.comfyui_upload_subfolder,
                        )
                    )
                finally:
                    try:
                        if os.path.exists(tmp_path) and os.path.isfile(tmp_path) and os.path.abspath(tmp_dir) in os.path.abspath(tmp_path):
                            os.remove(tmp_path)
                    except Exception:
                        pass
                changed = True
            elif is_file_ref(it):
                p = strip_file_ref(it)
                resolved_list.append(
                    await _upload_local_file_for_provider(
                        provider=provider,
                        comfyui=comfyui,
                        runninghub=runninghub,
                        file_path=p,
                        overwrite=ctx.settings.comfyui_upload_overwrite,
                        subfolder=ctx.settings.comfyui_upload_subfolder,
                    )
                )
                changed = True
            else:
                resolved_list.append(it)

        if not changed:
            continue

        if len(resolved_list) <= 1:
            out[k] = resolved_list[0] if resolved_list else ""
        else:
            if str(k) in (wf.params or {}):
                out[k] = resolved_list
            else:
                raise RuntimeError(f"param does not support multiple files (not in workflow params): {k}={v}")
    return out


_INLINE_NODE_KEY = re.compile(r"^(?P<node>\d+)\.(?P<field>[A-Za-z_][A-Za-z0-9_]*)$")


def _auto_cast(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (int, float, bool)):
        return v
    s = str(v).strip()
    if not s:
        return ""
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    if re.fullmatch(r"-?\d+", s):
        try:
            return int(s)
        except Exception:
            return s
    if re.fullmatch(r"-?\d+\.\d+", s):
        try:
            return float(s)
        except Exception:
            return s
    return s


def _build_inline_node_info_list(params: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for k, v in (params or {}).items():
        m = _INLINE_NODE_KEY.match(str(k))
        if not m:
            continue
        out.append({"nodeId": m.group("node"), "fieldName": m.group("field"), "fieldValue": _auto_cast(v)})
    return out


async def _mark_status(bitable: Any, record_id: str, status_key: str) -> None:
    if not bitable.mode.write_enabled or record_id.startswith("mock_rec_"):
        return
    cfg = bitable.config
    status_field = cfg.fields.get("status")
    if not status_field:
        return
    status_value = cfg.status_values.get(status_key)
    if not status_value:
        return
    await bitable.update_record(record_id, {status_field: status_value})


def _status_values_for_reset(cfg: Any, scope: str) -> set[str]:
    status_values = getattr(cfg, "status_values", None) or {}
    if not isinstance(status_values, dict):
        return set()
    if scope == "all":
        out0 = {str(v) for v in status_values.values() if v}
        return {x for x in out0 if x}
    if scope == "all_nonqueued":
        out = {str(v) for k, v in status_values.items() if k != "queued" and v}
        return {x for x in out if x}
    if scope == "running_failed":
        out2 = {str(status_values.get("running") or ""), str(status_values.get("failed") or "")}
        return {x for x in out2 if x}
    if scope == "failed_only":
        out3 = {str(status_values.get("failed") or "")}
        return {x for x in out3 if x}
    return set()


async def _reset_table_records(
    bitable: Any,
    *,
    scope: str,
    clear: bool,
) -> tuple[int, int, int, int, int, int, int, int]:
    if not bitable.mode.read_enabled or not bitable.mode.write_enabled:
        return 0, 0, 0, 0, 0, 0, 0, 0
    cfg = bitable.config
    status_field = cfg.fields.get("status")
    queued_value = cfg.status_values.get("queued")
    if not status_field or not queued_value:
        return 0, 0, 0, 0, 0, 0, 0, 0
    targets = _status_values_for_reset(cfg, scope)
    if not targets:
        return 0, 0, 0, 0, 0, 0, 0, 0
    output_field = cfg.fields.get("output")
    error_field = cfg.fields.get("error")
    prompt_field = cfg.fields.get("prompt_id")

    fields_meta: dict[str, dict[str, Any]] = {}
    if clear and hasattr(bitable, "list_fields"):
        try:
            meta_items = await bitable.list_fields()
        except Exception:
            meta_items = []
        for it in meta_items:
            if not isinstance(it, dict):
                continue
            name = it.get("field_name")
            if isinstance(name, str) and name:
                fields_meta[name] = it

    def _ui_type(field_name: str) -> str:
        meta = fields_meta.get(field_name) or {}
        ui = meta.get("ui_type")
        return str(ui) if ui is not None else ""

    def _is_writable(field_name: str) -> bool:
        ui = _ui_type(field_name)
        if not ui:
            return True
        return ui not in {"Formula", "AutoNumber", "CreatedTime", "LastModifiedTime", "Lookup", "Rollup", "Button", "Workflow"}

    def _is_empty(v: Any) -> bool:
        if v is None:
            return True
        if isinstance(v, str):
            return not v.strip()
        if isinstance(v, list):
            return len(v) == 0
        return False

    changed = 0
    cleared_prompt = 0
    cleared_error = 0
    cleared_output = 0
    failed_clear = 0
    skipped_prompt = 0
    skipped_error = 0
    skipped_output = 0
    page_token: str | None = None
    while True:
        items, page_token, has_more, _ = await bitable.list_records_page(view_id=cfg.view_id, page_size=200, page_token=page_token)
        for it in items:
            rid = it.get("record_id")
            if not rid:
                continue
            fields = it.get("fields") if isinstance(it.get("fields"), dict) else {}
            cur = fields.get(status_field)
            if cur not in targets:
                continue
            await bitable.update_record(str(rid), {status_field: queued_value})
            if clear:
                if prompt_field and not _is_writable(prompt_field):
                    skipped_prompt += 1
                if error_field and not _is_writable(error_field):
                    skipped_error += 1
                if output_field and not _is_writable(output_field):
                    skipped_output += 1
                if prompt_field and _is_writable(prompt_field):
                    for v in ("", None):
                        try:
                            await bitable.update_record(str(rid), {prompt_field: v})
                            break
                        except Exception:
                            continue
                if error_field and _is_writable(error_field):
                    for v in ("", None):
                        try:
                            await bitable.update_record(str(rid), {error_field: v})
                            break
                        except Exception:
                            continue
                if output_field and _is_writable(output_field):
                    for v in ([], None, ""):
                        try:
                            await bitable.update_record(str(rid), {output_field: v})
                            break
                        except Exception:
                            continue
                try:
                    rec = await bitable.get_record(str(rid))
                except Exception:
                    rec = {}
                rec_fields = rec.get("fields") if isinstance(rec, dict) and isinstance(rec.get("fields"), dict) else {}
                ok_this = True
                if prompt_field:
                    if _is_empty(rec_fields.get(prompt_field)):
                        cleared_prompt += 1
                    else:
                        ok_this = False if _is_writable(prompt_field) else ok_this
                if error_field:
                    if _is_empty(rec_fields.get(error_field)):
                        cleared_error += 1
                    else:
                        ok_this = False if _is_writable(error_field) else ok_this
                if output_field:
                    if _is_empty(rec_fields.get(output_field)):
                        cleared_output += 1
                    else:
                        ok_this = False if _is_writable(output_field) else ok_this
                if not ok_this:
                    failed_clear += 1
            changed += 1
        if not has_more or not page_token:
            break
    return changed, cleared_prompt, cleared_error, cleared_output, failed_clear, skipped_prompt, skipped_error, skipped_output


async def run_workflow(
    ctx: AppContext,
    *,
    trigger: TriggerContext,
    workflow_key: str,
    record_id: str | None,
    row: int | None,
    view_id: str | None,
    params: dict[str, Any],
    table_key: str | None,
) -> str | None:
    resolved_table_key = table_key or ctx.default_table_key
    cfg_for_wf = (ctx.config.get("workflows") or {}).get(workflow_key) or {}
    if not table_key and isinstance(cfg_for_wf, dict):
        tk = cfg_for_wf.get("table")
        if isinstance(tk, str) and tk:
            resolved_table_key = tk

    provider = str(cfg_for_wf.get("provider") or "comfyui").strip().lower() if isinstance(cfg_for_wf, dict) else "comfyui"

    bitable = ctx.bitables.get(resolved_table_key) if resolved_table_key else None
    table_cfg = ctx.bitable_configs.get(resolved_table_key) if resolved_table_key else None
    app_token = table_cfg.app_token if table_cfg else None
    table_id = table_cfg.table_id if table_cfg else None
    write_back = bool(bitable and bitable.mode.write_enabled and table_cfg)
    comfyui_base_url = ctx.settings.comfyui_base_url
    wf = ctx.workflows.get(workflow_key)
    if isinstance(cfg_for_wf, dict):
        override_base = cfg_for_wf.get("comfyuiBaseUrl") or cfg_for_wf.get("comfyui_base_url") or cfg_for_wf.get("comfyui_base")
        if isinstance(override_base, str) and override_base.strip():
            comfyui_base_url = override_base.strip()
    comfyui_client = ctx.comfyui if comfyui_base_url.rstrip("/") == ctx.settings.comfyui_base_url.rstrip("/") else ComfyUIClient(comfyui_base_url)
    runninghub_client = RunningHubClient(api_key=str(ctx.settings.runninghub_api_key or "")) if provider == "runninghub" else None

    if not record_id and row:
        if not (bitable and table_cfg and bitable.mode.read_enabled):
            raise RuntimeError(f"未找到指定记录 (row={row})")
        resolved_view_id = view_id or table_cfg.view_id
        page_token: str | None = None
        offset = 0
        while True:
            items, page_token, has_more, _ = await bitable.list_records_page(view_id=resolved_view_id, page_size=200, page_token=page_token)
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
                
    if not record_id and not row:
        if bitable and bitable.mode.read_enabled:
            record_id = await bitable.find_next_queued_record_id()

    if not record_id and bitable:
        raise RuntimeError(f"未找到指定记录 (row={row})")

    if not wf:
        err_msg = f"未找到工作流配置: {workflow_key}"
        if record_id and not record_id.startswith("mock_rec_") and bitable and table_cfg and bitable.mode.write_enabled:
            error_field = table_cfg.fields.get("error")
            status_field = table_cfg.fields.get("status")
            failed_value = table_cfg.status_values.get("failed")
            updates: dict[str, Any] = {}
            if error_field:
                updates[error_field] = err_msg
            if status_field and failed_value:
                updates[status_field] = failed_value
            if updates:
                try:
                    await bitable.update_record(record_id, updates)
                except Exception:
                    pass
        raise RuntimeError(err_msg)

    inline_node_info_list = _build_inline_node_info_list(params)
    extra_data = {
        "callback_url": _pick_callback_url_for_base(ctx, comfyui_base_url),
        "callback_context": {
            "record_id": record_id,
            "recordId": record_id,
            "workflow": workflow_key,
            "tableKey": resolved_table_key,
            "chat_id": trigger.chat_id,
            "user_open_id": trigger.user_open_id,
            "source": trigger.source,
            "appToken": app_token,
            "tableId": table_id,
            "writeBack": write_back and not record_id.startswith("mock_rec_") if record_id else write_back,
            "comfyui_base_url": comfyui_base_url,
        },
    }

    merged: dict[str, Any] = {}
    record_fields: dict[str, Any] = {}
    use_record_fields = bool(
        record_id
        and bitable
        and bitable.mode.read_enabled
        and (ctx.settings.bitable_mode or "").strip().lower() not in ("write", "writeonly", "wo")
    )
    if use_record_fields:
        rec = await bitable.get_record(record_id)
        record_fields = _extract_record_fields(rec)

    raw_cfg = (ctx.config.get("workflows") or {}).get(wf.key) or {}
    raw_defaults = raw_cfg.get("defaults") or {}
    if isinstance(raw_defaults, dict):
        merged.update(raw_defaults)
    if use_record_fields:
        record_field_map = raw_cfg.get("recordFields") or {}
        for param_key in wf.params.keys():
            field_name = record_field_map.get(param_key) or param_key
            if field_name in record_fields:
                raw_val = record_fields.get(field_name)
                downloaded = await _download_attachments(
                    ctx,
                    provider=provider,
                    comfyui=comfyui_client,
                    runninghub=runninghub_client,
                    value=raw_val,
                )
                v = downloaded if downloaded else raw_val
                if isinstance(merged.get(param_key), list) and isinstance(v, list):
                    base = list(v)
                    default_list = list(merged.get(param_key) or [])
                    if len(base) < len(default_list):
                        base.extend(default_list[len(base):])
                    merged[param_key] = base
                else:
                    merged[param_key] = v

    if record_id:
        if "save_prefix_1" in wf.params and "save_prefix_1" not in params:
            merged["save_prefix_1"] = f"ComfyUI_out_1_{record_id}"
        if "save_prefix_2" in wf.params and "save_prefix_2" not in params:
            merged["save_prefix_2"] = f"ComfyUI_out_2_{record_id}"

    params = await _resolve_file_refs_in_params(
        ctx,
        trigger=trigger,
        provider=provider,
        comfyui=comfyui_client,
        runninghub=runninghub_client,
        wf=wf,
        params=params,
    )

    merged.update(params)

    temp_files: list[str] = []
    base_download_dir = (ctx.settings.bitable_download_dir or "").strip()
    if base_download_dir:
        base_abs = os.path.abspath(base_download_dir)
        for s in _collect_file_paths(merged):
            try:
                p = str(s or "").strip()
                if not p:
                    continue
                ap = os.path.abspath(p)
                if not ap.startswith(base_abs):
                    continue
                if os.path.exists(ap) and os.path.isfile(ap):
                    temp_files.append(ap)
            except Exception:
                continue

    node_info_list = ctx.workflows.build_node_info_list(wf, merged)
    inline_node_info_list = _build_inline_node_info_list(params)
    if inline_node_info_list:
        node_info_list = node_info_list + inline_node_info_list
    extra_data = {
        "callback_url": _pick_callback_url_for_base(ctx, comfyui_base_url),
        "callback_context": {
            "record_id": record_id,
            "recordId": record_id,
            "workflow": workflow_key,
            "tableKey": resolved_table_key,
            "chat_id": trigger.chat_id,
            "user_open_id": trigger.user_open_id,
            "source": trigger.source,
            "appToken": app_token,
            "tableId": table_id,
            "writeBack": write_back and not record_id.startswith("mock_rec_") if record_id else write_back,
            "comfyui_base_url": comfyui_base_url,
        },
    }

    prompt_id: str | None = None
    err_msg: str | None = None
    try:
        if provider == "runninghub":
            mode = str(getattr(ctx.settings, "remote_result_mode", "") or "").strip().lower()
            webhook_url = "" if mode == "poll" else (ctx.settings.remote_callback_url or "").strip()
            if mode != "poll" and not webhook_url:
                raise RuntimeError("missing REMOTE_CALLBACK_URL")
            rh = (cfg_for_wf.get("runninghub") or {}) if isinstance(cfg_for_wf, dict) else {}
            if not isinstance(rh, dict):
                rh = {}
            workflow_id = str(rh.get("workflowId") or cfg_for_wf.get("workflowId") or "").strip()
            if not workflow_id:
                raise RuntimeError("missing runninghub workflowId")
            created = await runninghub_client.create_task(
                workflow_id=workflow_id,
                node_info_list=node_info_list,
                webhook_url=webhook_url or None,
                add_metadata=rh.get("addMetadata"),
                workflow=rh.get("workflow"),
                instance_type=rh.get("instanceType"),
                use_personal_queue=rh.get("usePersonalQueue"),
                retain_seconds=rh.get("retainSeconds"),
                access_password=rh.get("accessPassword"),
            )
            prompt_id = created.task_id
        else:
            prompt_id = await queue_by_workflowprompt(
                comfyui_client,
                wf=wf,
                node_info_list=node_info_list,
                extra_data=extra_data,
            )
            if prompt_id and temp_files and getattr(ctx, "runner", None) and hasattr(ctx.runner, "register_temp_files"):
                try:
                    await ctx.runner.register_temp_files(prompt_id=prompt_id, file_paths=temp_files)
                except Exception:
                    pass
    except httpx.HTTPStatusError as e:
        if provider == "runninghub":
            err_msg = f"HTTPError {e.response.status_code if e.response else 'unknown'}: {e}"
        elif e.response is not None and e.response.status_code == 404:
            if not wf.api_workflow_path:
                err_msg = f"WorkflowPrompt 返回 404（可能插件未安装，或 workflowName [{wf.workflow_name}] 在该 ComfyUI 上不存在），且该 workflow 未配置 apiWorkflowPath"
            else:
                try:
                    prompt = json.loads(Path(wf.api_workflow_path).read_text(encoding="utf-8"))
                    if isinstance(prompt, dict):
                        for it in node_info_list:
                            node_id = str(it.get("nodeId") or "")
                            field_name = str(it.get("fieldName") or "")
                            if not node_id or not field_name:
                                continue
                            node = prompt.get(node_id)
                            if not isinstance(node, dict):
                                continue
                            inputs = node.get("inputs")
                            if not isinstance(inputs, dict):
                                continue
                            inputs[field_name] = it.get("fieldValue")
                    if provider != "runninghub":
                        res = await comfyui_client.queue_api_prompt(prompt=prompt, extra_data=extra_data)
                        prompt_id = res.prompt_id
                        err_msg = None
                except Exception as e2:
                    err_msg = f"降级执行 api_workflow_path 失败: {e2}"
        else:
            err_msg = f"HTTPError {e.response.status_code if e.response else 'unknown'}: {e}"
    except Exception as e:
        err_msg = str(e)

    if prompt_id and temp_files and getattr(ctx, "runner", None) and hasattr(ctx.runner, "register_temp_files"):
        try:
            await ctx.runner.register_temp_files(prompt_id=prompt_id, file_paths=temp_files)
        except Exception:
            pass

    if prompt_id and getattr(ctx, "runner", None):
        if hasattr(ctx.runner, "register_prompt_context"):
            try:
                await ctx.runner.register_prompt_context(
                    prompt_id=prompt_id,
                    record_id=record_id,
                    table_key=resolved_table_key,
                    workflow_key=workflow_key,
                    chat_id=trigger.chat_id,
                )
            except Exception:
                pass
        if hasattr(ctx.runner, "register_pending_remote"):
            try:
                if provider == "runninghub" or (provider != "runninghub" and not _is_local_base_url(comfyui_base_url)):
                    await ctx.runner.register_pending_remote(prompt_id=prompt_id, provider=provider, comfyui_base_url=comfyui_base_url)
            except Exception:
                pass

    if err_msg:
        if record_id and not record_id.startswith("mock_rec_") and bitable and table_cfg and bitable.mode.write_enabled:
            error_field = table_cfg.fields.get("error")
            status_field = table_cfg.fields.get("status")
            failed_value = table_cfg.status_values.get("failed")
            updates: dict[str, Any] = {}
            if error_field:
                updates[error_field] = err_msg
            if status_field and failed_value:
                updates[status_field] = failed_value
            if updates:
                try:
                    await bitable.update_record(record_id, updates)
                except Exception:
                    pass
        raise RuntimeError(err_msg)

    if prompt_id and record_id and not record_id.startswith("mock_rec_") and bitable and table_cfg and bitable.mode.write_enabled:
        updates: dict[str, Any] = {}
        status_field = table_cfg.fields.get("status")
        running_value = table_cfg.status_values.get("running")
        if status_field and running_value:
            updates[status_field] = running_value
        prompt_field = table_cfg.fields.get("prompt_id")
        if prompt_field:
            updates[prompt_field] = prompt_id
        if updates:
            try:
                await bitable.update_record(record_id, updates)
            except Exception:
                pass
    return prompt_id


async def dispatch(ctx: AppContext, *, name: str, args: dict[str, Any], trigger: TriggerContext) -> None:
    im = IMClient(ctx.auth)

    if name in ("help", "h"):
        if trigger.chat_id:
            await handle_help(im, trigger.chat_id)
        return

    if name == "ids":
        if trigger.chat_id:
            chat_id = str(trigger.chat_id or "")
            user_open_id = str(trigger.user_open_id or "")
            await im.send_text(chat_id=trigger.chat_id, text=f"chat_id={chat_id}\nuser_open_id={user_open_id}")
        return

    if name == "botid":
        if not trigger.chat_id:
            return
        token = ""
        try:
            token = await ctx.auth.tenant_token()
        except Exception as e:
            await im.send_text(chat_id=trigger.chat_id, text=f"bot_id error: {e}")
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    "https://open.feishu.cn/open-apis/bot/v3/info/",
                    headers={"Authorization": f"Bearer {token}"},
                )
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            await im.send_text(chat_id=trigger.chat_id, text=f"bot_id error: {e}")
            return
        bot_open_id = None
        try:
            d0 = data.get("data") if isinstance(data, dict) else None
            if isinstance(d0, dict):
                b0 = d0.get("bot")
                if isinstance(b0, dict):
                    for k in ("open_id", "openId", "bot_open_id", "botOpenId"):
                        v = b0.get(k)
                        if isinstance(v, str) and v.strip():
                            bot_open_id = v.strip()
                            break
                if not bot_open_id:
                    for k in ("open_id", "openId", "bot_open_id", "botOpenId"):
                        v = d0.get(k)
                        if isinstance(v, str) and v.strip():
                            bot_open_id = v.strip()
                            break
        except Exception:
            bot_open_id = None
        if bot_open_id:
            await im.send_text(chat_id=trigger.chat_id, text=f"bot_open_id={bot_open_id}")
        else:
            await im.send_text(chat_id=trigger.chat_id, text=f"bot_open_id not found: {data}")
        return

    if name == "cb":
        sig = str(args.get("sig") or args.get("token") or "").strip()
        if ctx.settings.cb_message_token and sig != ctx.settings.cb_message_token:
            if trigger.chat_id:
                await im.send_text(chat_id=trigger.chat_id, text="cb token mismatch")
            return
        payload: dict[str, Any] | None = None
        raw_data = str(args.get("data") or "").strip()
        if raw_data:
            payload = _b64url_json_decode(raw_data)
            if not payload:
                if trigger.chat_id:
                    await im.send_text(chat_id=trigger.chat_id, text="cb invalid data")
                return
        else:
            provider = str(args.get("provider") or args.get("p") or "comfyui").strip().lower()
            pid = args.get("id") or args.get("prompt_id") or args.get("promptId") or args.get("taskId")
            pid = str(pid).strip() if isinstance(pid, (str, int)) else ""
            if not pid:
                if trigger.chat_id:
                    await im.send_text(chat_id=trigger.chat_id, text="cb missing id")
                return
            payload = {"provider": provider, "prompt_id": pid, "completed": True}
            if provider == "runninghub":
                try:
                    cli = RunningHubClient(api_key=str(ctx.settings.runninghub_api_key or ""))
                    q = await cli.query_results_v2(task_id=pid)
                except Exception as e:
                    if trigger.chat_id:
                        await im.send_text(chat_id=trigger.chat_id, text=f"cb runninghub query error: {e}")
                    return
                st = (q.status or "").strip().upper()
                if st in ("SUCCESS", "SUCCEEDED", "OK"):
                    payload["status"] = "success"
                    payload["completed"] = True
                elif st in ("FAILED", "FAILURE", "ERROR"):
                    payload["status"] = "failed"
                    payload["completed"] = True
                    if q.error_message:
                        payload["errorMessage"] = q.error_message
                    if q.error_code:
                        payload["errorCode"] = q.error_code
                    fr = q.raw.get("failedReason") if isinstance(q.raw, dict) else None
                    if isinstance(fr, dict):
                        payload["failedReason"] = fr
                else:
                    payload["status"] = st.lower() if st else ""
                    payload["completed"] = False
                files: list[dict[str, Any]] = []
                for it in q.results:
                    url = it.get("url")
                    if isinstance(url, str) and url.strip():
                        files.append({"url": url.strip(), "outputType": it.get("outputType")})
                if files:
                    payload["files"] = files
                if payload.get("completed") is False:
                    if trigger.chat_id:
                        await im.send_text(chat_id=trigger.chat_id, text=f"cb still running: {pid}")
                    return
        prompt_id = payload.get("prompt_id") or payload.get("promptId")
        prompt_id = str(prompt_id).strip() if isinstance(prompt_id, str) else ""
        if trigger.chat_id:
            ctx0 = payload.get("context")
            if not isinstance(ctx0, dict):
                ctx0 = {}
                payload["context"] = ctx0
            if not isinstance(ctx0.get("chat_id"), str):
                ctx0["chat_id"] = trigger.chat_id
            await im.send_text(chat_id=trigger.chat_id, text=f"cb received{(' prompt=' + prompt_id) if prompt_id else ''}")
        try:
            await handle_callback_payload(ctx, payload)
        except Exception as e:
            if trigger.chat_id:
                await im.send_text(chat_id=trigger.chat_id, text=f"cb error: {e}")
        return

    if name == "panel":
        if trigger.chat_id:
            await im.send_interactive_card(chat_id=trigger.chat_id, card=build_panel_card())
        return

    if name in ("reset", "reset_table"):
        table_key = _pick_table_key(args) or ctx.default_table_key
        bitable = ctx.bitables.get(table_key) if table_key else None
        if not bitable:
            if trigger.chat_id:
                if (not ctx.bitable_mode.read_enabled) and ctx.bitable_configs and (ctx.settings.bitable_mode or "").strip().lower() not in ("off", "none", "disable", "disabled"):
                    await _send_license_guidance(im, trigger.chat_id)
                else:
                    await im.send_text(chat_id=trigger.chat_id, text="缺少 table 或 table 不存在")
            return
        scope = str(args.get("scope") or "all_nonqueued").strip()
        clear = str(args.get("clear") or "").strip().lower() in ("1", "true", "yes", "y", "on")
        n, cp, ce, co, fc, sp, se, so = await _reset_table_records(bitable, scope=scope, clear=clear)
        if trigger.chat_id:
            if clear:
                await im.send_text(chat_id=trigger.chat_id, text=f"已重置: {n} 清空(任务ID:{cp} 错误:{ce} 结果:{co} 失败:{fc} 不可写:任务ID:{sp} 错误:{se} 结果:{so})")
            else:
                await im.send_text(chat_id=trigger.chat_id, text=f"已重置: {n}")
        return

    if name == "run_default":
        record_id: str | None = None
        default_table_key = ctx.default_table_key
        bitable = ctx.bitables.get(default_table_key) if default_table_key else None
        
        if bitable and bitable.mode.read_enabled:
            record_id = await bitable.find_next_queued_record_id()
        workflow_key = _pick_default_workflow_key(ctx, table_key=default_table_key)
        if not workflow_key:
            if trigger.chat_id:
                await im.send_text(chat_id=trigger.chat_id, text="未找到默认工作流配置")
            return
                
        prompt_id = await run_workflow(
            ctx,
            trigger=trigger,
            workflow_key=workflow_key,
            record_id=record_id,
            row=None,
            view_id=None,
            params={},
            table_key=default_table_key,
        )
        if trigger.chat_id:
            await im.send_text(chat_id=trigger.chat_id, text=f"已入队: {prompt_id or 'unknown'}")
        return

    if name in ("run", "wf"):
        record_id = _pick_record_id(args)
        row = _pick_row(args)
        view_id = _pick_view_id(args)
        table_key = _pick_table_key(args)
        workflow_key = _pick_workflow_key(args) if name == "wf" else (args.get("workflow") or "default")
        workflow_key = str(workflow_key) if workflow_key else "default"

        if row and trigger.chat_id and (not ctx.bitable_mode.read_enabled) and ctx.bitable_configs and (ctx.settings.bitable_mode or "").strip().lower() not in ("off", "none", "disable", "disabled"):
            await _send_license_guidance(im, trigger.chat_id)
            return
        
        if workflow_key == "default" and not ctx.workflows.get("default"):
            resolved_table_key = table_key or ctx.default_table_key
            picked = _pick_default_workflow_key(ctx, table_key=resolved_table_key)
            if picked:
                workflow_key = picked

        if not record_id and not row:
            resolved_table_key = table_key or ctx.default_table_key
            bitable = ctx.bitables.get(resolved_table_key) if resolved_table_key else None
            
            if bitable and bitable.mode.read_enabled:
                record_id = await bitable.find_next_queued_record_id()

        params = _args_without_meta(args)
        prompt_id = await run_workflow(
            ctx,
            trigger=trigger,
            workflow_key=workflow_key,
            record_id=record_id,
            row=row,
            view_id=view_id,
            params=params,
            table_key=table_key,
        )
        if trigger.chat_id:
            await im.send_text(chat_id=trigger.chat_id, text=f"已入队: {prompt_id or 'unknown'}")
        return

    if name in ("batch", "drain"):
        base_table_key = _pick_table_key(args) or ctx.default_table_key
        workflow_key = _pick_workflow_key(args) or str(args.get("workflow") or "")
        
        if not workflow_key:
            workflow_key = _pick_default_workflow_key(ctx, table_key=base_table_key) or "default"
        
        if not workflow_key or (workflow_key == "default" and not ctx.workflows.get("default")):
            if trigger.chat_id:
                await im.send_text(chat_id=trigger.chat_id, text="缺少 workflow 且未找到默认工作流配置")
            return
            
        table_key = _pick_table_key_for_workflow(ctx, args=args, workflow_key=workflow_key)
        if not table_key:
            if trigger.chat_id:
                await im.send_text(chat_id=trigger.chat_id, text="缺少 table")
            return
            
        batch = int(str(args.get("batch") or args.get("limit") or "10"))
        inflight = int(str(args.get("inflight") or "1"))
        
        bitable = ctx.bitables.get(table_key)
        if bitable is None or not bitable.mode.read_enabled:
            if trigger.chat_id:
                if (not ctx.bitable_mode.read_enabled) and ctx.bitable_configs and (ctx.settings.bitable_mode or "").strip().lower() not in ("off", "none", "disable", "disabled"):
                    await _send_license_guidance(im, trigger.chat_id)
                else:
                    await im.send_text(chat_id=trigger.chat_id, text="无法读取表格，无法执行批量(batch/drain)操作。")
            return
        if trigger.chat_id:
            await im.send_text(chat_id=trigger.chat_id, text=f"已启动队列: {workflow_key} table={table_key}")
        try:
            await ctx.runner.start(
                workflow_key=workflow_key,
                table_key=table_key,
                batch=batch,
                inflight=inflight,
                drain=(name == "drain"),
                chat_id=trigger.chat_id,
            )
        except Exception as e:
            if trigger.chat_id:
                await im.send_text(chat_id=trigger.chat_id, text=f"队列启动失败: {e}")
        return

    if name == "stop_queue":
        workflow_key = _pick_workflow_key(args) or str(args.get("workflow") or "")
        base_table_key = _pick_table_key(args) or ctx.default_table_key
        
        if not workflow_key:
            workflow_key = _pick_default_workflow_key(ctx, table_key=base_table_key) or "default"

        table_key = _pick_table_key_for_workflow(ctx, args=args, workflow_key=workflow_key)
        if workflow_key and table_key:
            await ctx.runner.stop(workflow_key=workflow_key, table_key=table_key)
            if trigger.chat_id:
                await im.send_text(chat_id=trigger.chat_id, text=f"已停止队列: {workflow_key} table={table_key}")
        return

    if trigger.chat_id:
        await im.send_text(chat_id=trigger.chat_id, text=f"未知指令: {name}")


def dispatch_in_thread(ctx: AppContext, *, name: str, args: dict[str, Any], trigger: TriggerContext) -> None:
    try:
        asyncio.run(dispatch(ctx, name=name, args=args, trigger=trigger))
    except Exception as e:
        logging.exception("dispatch failed: %s", e)
        if trigger.chat_id:
            try:
                asyncio.run(IMClient(ctx.auth).send_text(chat_id=trigger.chat_id, text=f"执行失败: {e}"))
            except Exception:
                pass
