from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import logging
import time
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
from .modules.bitable_logic import (
    create_record as _enc_bitable_create_record,
    map_fields_by_config as _enc_map_fields_by_config,
    mark_status as _enc_mark_status,
    reset_table_records as _enc_reset_table_records,
    resolve_relation_param_items as _enc_resolve_relation_param_items,
    resolve_relation_prompts as _enc_resolve_relation_prompts,
)
from .runninghub import RunningHubClient
from .workflows import WorkflowSpec

@dataclass(frozen=True)
class TriggerContext:
    chat_id: str | None
    user_open_id: str | None
    source: str


def _default_panel_spec() -> dict[str, Any]:
    return {
        "title": "ComfyUI 控制面板",
        "rows": [
            [
                {"text": "运行默认流程", "type": "primary", "cmd": "run_default", "args": {}},
                {"text": "执行队列(drain)", "type": "danger", "cmd": "drain", "args": {}},
            ]
        ],
    }


def _load_panel_spec_from_config(cfg: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(cfg, dict):
        return _default_panel_spec()
    raw = cfg.get("panel")
    if not isinstance(raw, dict):
        return _default_panel_spec()
    title = raw.get("title")
    title = str(title).strip() if isinstance(title, str) else ""
    rows0 = raw.get("rows")
    if not isinstance(rows0, list):
        return _default_panel_spec()

    rows: list[list[dict[str, Any]]] = []
    for row in rows0:
        if not isinstance(row, list):
            continue
        out_row: list[dict[str, Any]] = []
        for btn in row:
            if not isinstance(btn, dict):
                continue
            text = btn.get("text")
            cmd = btn.get("cmd")
            if not isinstance(text, str) or not text.strip():
                continue
            if not isinstance(cmd, str) or not cmd.strip():
                continue
            type0 = btn.get("type")
            type_s = str(type0).strip().lower() if isinstance(type0, str) else ""
            if type_s not in ("default", "primary", "danger"):
                type_s = "default"
            args0 = btn.get("args")
            args: dict[str, Any] = args0 if isinstance(args0, dict) else {}
            out_row.append({"text": text.strip(), "type": type_s, "cmd": cmd.strip(), "args": args})
        if out_row:
            rows.append(out_row)

    if not rows:
        return _default_panel_spec()
    return {"title": title or _default_panel_spec()["title"], "rows": rows}


def build_panel_card(ctx: AppContext | None = None) -> dict[str, Any]:
    spec = _load_panel_spec_from_config(getattr(ctx, "config", None) if ctx else None)
    title = str(spec.get("title") or "ComfyUI 控制面板").strip() or "ComfyUI 控制面板"
    rows = spec.get("rows") if isinstance(spec.get("rows"), list) else []

    elements: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        actions: list[dict[str, Any]] = []
        for btn in row:
            if not isinstance(btn, dict):
                continue
            text = str(btn.get("text") or "").strip()
            cmd = str(btn.get("cmd") or "").strip()
            if not text or not cmd:
                continue
            type_s = str(btn.get("type") or "default").strip().lower()
            if type_s not in ("default", "primary", "danger"):
                type_s = "default"
            args0 = btn.get("args")
            args = args0 if isinstance(args0, dict) else {}
            value: dict[str, Any] = {"cmd": cmd}
            if args:
                value["args"] = args
            actions.append({"tag": "button", "text": {"tag": "plain_text", "content": text}, "type": type_s, "value": value})
        if actions:
            elements.append({"tag": "action", "actions": actions})

    if not elements:
        elements = [
            {
                "tag": "action",
                "actions": [
                    {"tag": "button", "text": {"tag": "plain_text", "content": "运行默认流程"}, "type": "primary", "value": {"cmd": "run_default"}},
                    {"tag": "button", "text": {"tag": "plain_text", "content": "执行队列(drain)"}, "type": "danger", "value": {"cmd": "drain"}},
                ],
            }
        ]

    return {"config": {"wide_screen_mode": True}, "header": {"title": {"tag": "plain_text", "content": title}}, "elements": elements}


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


def _workflow_bound_table_key(ctx: AppContext, workflow_key: str | None) -> str | None:
    wk = str(workflow_key or "").strip()
    if wk:
        wf_cfg = (ctx.config.get("workflows") or {}).get(wk) or {}
        if isinstance(wf_cfg, dict):
            v = wf_cfg.get("table")
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _pick_table_key_for_workflow(
    ctx: AppContext,
    *,
    args: dict[str, Any],
    workflow_key: str | None,
    allow_default: bool = True,
) -> str | None:
    t = _pick_table_key(args)
    if t:
        return t
    bound = _workflow_bound_table_key(ctx, workflow_key)
    if bound:
        return bound
    return ctx.default_table_key if allow_default else None


def _should_fallback_to_api_workflow(status_code: int | None) -> bool:
    """只在 WorkflowPrompt 明确不可用/工作流不存在时才走 apiWorkflowPath 降级。"""
    return int(status_code or 0) == 404


def _parse_json_object(text: str) -> dict[str, Any] | None:
    s = str(text or "").strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _looks_like_workflow_missing(body_text: str, body_obj: dict[str, Any] | None) -> bool:
    hay = str(body_text or "").lower()
    msg_parts: list[str] = [hay]
    if isinstance(body_obj, dict):
        err = body_obj.get("error")
        if isinstance(err, dict):
            msg_parts.extend([
                str(err.get("type") or "").lower(),
                str(err.get("message") or "").lower(),
                str(err.get("details") or "").lower(),
            ])
    joined = "\n".join(msg_parts)
    patterns = (
        "workflow not found",
        "workflow does not exist",
        "unknown workflow",
        "cannot find workflow",
        "no workflow",
        "route not found",
        "not found",
        "plugin not installed",
    )
    return any(p in joined for p in patterns)


def _extract_first_node_error(body_obj: dict[str, Any] | None) -> tuple[str | None, str | None, str | None]:
    if not isinstance(body_obj, dict):
        return None, None, None
    node_errors = body_obj.get("node_errors")
    if not isinstance(node_errors, dict):
        return None, None, None
    for node_id, node_info in node_errors.items():
        if not isinstance(node_info, dict):
            continue
        errs = node_info.get("errors")
        if not isinstance(errs, list) or not errs:
            continue
        first = errs[0]
        if not isinstance(first, dict):
            continue
        return str(node_id), str(first.get("message") or ""), str(first.get("details") or "")
    return None, None, None


def _build_workflowprompt_user_error(
    *,
    workflow_name: str,
    status_code: int | None,
    body_text: str,
) -> tuple[str, bool]:
    body_obj = _parse_json_object(body_text)
    fallback_allowed = _should_fallback_to_api_workflow(status_code) or _looks_like_workflow_missing(body_text, body_obj)

    error_block = body_obj.get("error") if isinstance(body_obj, dict) else None
    error_type = str(error_block.get("type") or "") if isinstance(error_block, dict) else ""
    error_message = str(error_block.get("message") or "") if isinstance(error_block, dict) else ""
    error_details = str(error_block.get("details") or "") if isinstance(error_block, dict) else ""
    node_id, node_message, node_details = _extract_first_node_error(body_obj)

    if node_id or error_type == "prompt_outputs_failed_validation":
        hint = "请检查你传入的参数值是否真的有效。"
        if "invalid image file" in str(node_details or "").lower():
            hint = (
                "图片参数不对：`image/images` 需要传一张真正能读取到的图片。"
                "如果你是直接用聊天里的上一张图，可以写 `image=@msg:last`；"
                "如果你要传本机文件，可以写 `image=@E:\\pics\\a.jpg`。"
            )
        elif node_details:
            hint = f"参数检查没通过：{node_details}"
        msg = f"工作流参数检查没通过。{hint}"
        if node_id:
            msg += f"\n出错节点：{node_id}"
        return msg, False

    if fallback_allowed:
        msg = (
            f"找不到 WorkflowPrompt 里的工作流 `{workflow_name}`，或者 WorkflowPrompt 插件当前不可用。"
            "\n我会尝试改走 `apiWorkflowPath` 降级执行。"
        )
        return msg, True

    summary = error_message or error_details or body_text.strip()
    if summary:
        summary = summary[:200]
        return f"执行失败：ComfyUI 返回了错误。{summary}", False
    code = status_code if status_code is not None else "unknown"
    return f"执行失败：ComfyUI 返回错误（HTTP {code}）。", False


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
    await im.send_text(chat_id=chat_id, text=get_help_text())


def get_help_text() -> str:
    return (
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
    )


async def _send_text_by_trigger(im: IMClient, trigger: TriggerContext, text: str) -> None:
    if trigger.chat_id:
        await im.send_text(chat_id=trigger.chat_id, text=text)
        return
    if trigger.user_open_id:
        await im.send_text_to_open_id(open_id=trigger.user_open_id, text=text)


async def _send_card_by_trigger(im: IMClient, trigger: TriggerContext, card: dict[str, Any]) -> None:
    if trigger.chat_id:
        await im.send_interactive_card(chat_id=trigger.chat_id, card=card)
        return
    if trigger.user_open_id:
        await im.send_interactive_card_to_open_id(open_id=trigger.user_open_id, card=card)


async def _send_license_guidance(im: IMClient, trigger: TriggerContext) -> None:
    lic = check_license()
    await _send_text_by_trigger(
        im,
        trigger,
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
        for k in (
            "text",
            "name",
            "title",
            "label",
            "display_value",
            "displayValue",
            "value",
        ):
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


def _normalize_bitable_value_for_param(value: Any, spec: WorkflowSpec | None, param_key: str) -> Any:
    if not spec or not isinstance(param_key, str) or not param_key:
        return value
    ps = spec.params.get(param_key)
    if not ps:
        return value

    wants_list = bool(ps.multi or any(t.index is not None for t in ps.targets))
    type_name = str(ps.type or "str")

    if not isinstance(value, (list, dict)):
        if wants_list and value is not None and not isinstance(value, list):
            return [value]
        return value

    items = _collect_display_strings(value)
    if not items:
        return [] if wants_list and isinstance(value, list) else value

    if wants_list:
        return items

    if type_name == "str":
        return "，".join([x for x in items if str(x).strip()])
    return items[0]


def _extract_relation_record_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        for k in ("record_id", "recordId", "id"):
            v = value.get(k)
            if isinstance(v, str) and v.strip():
                return [v.strip()]
        for k in ("record_ids", "recordIds", "record_ids_list", "recordIdList"):
            v2 = value.get(k)
            if isinstance(v2, list):
                out0: list[str] = []
                for x in v2:
                    s = str(x or "").strip()
                    if s:
                        out0.append(s)
                if out0:
                    return out0
        return []
    if isinstance(value, list):
        out: list[str] = []
        for it in value:
            out.extend(_extract_relation_record_ids(it))
        return out
    return []


def _extract_relation_display_keys(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        return [s] if s else []
    if isinstance(value, list):
        out: list[str] = []
        for it in value:
            out.extend(_extract_relation_display_keys(it))
        return out
    if isinstance(value, dict):
        for k in ("name", "title", "text", "value", "label"):
            v = value.get(k)
            if isinstance(v, str) and v.strip():
                return [v.strip()]
        out2: list[str] = []
        for vv in value.values():
            out2.extend(_extract_relation_display_keys(vv))
        return out2
    s2 = str(value).strip()
    return [s2] if s2 else []


async def _search_one_record_by_field(
    bitable: Any,
    *,
    field_name: str,
    value: str,
) -> dict[str, Any] | None:
    fn = str(field_name or "").strip()
    vv = str(value or "").strip()
    if not fn or not vv:
        return None
    for op in ("is", "contains"):
        try:
            items = await bitable.search_records(
                filter_={
                    "conjunction": "and",
                    "conditions": [{"field_name": fn, "operator": op, "value": [vv]}],
                },
                page_size=1,
            )
        except Exception:
            items = []
        if items:
            return items[0] if isinstance(items[0], dict) else None
    return None


async def _resolve_relation_prompts(
    ctx: AppContext,
    *,
    source_value: Any,
    target_app_token: str | None,
    target_table_id: str | None,
    target_table_key: str | None,
    target_match_field: str | None,
    prompt_fields: list[str],
    join_with: str,
    max_items: int,
    strict: bool,
) -> list[str]:
    return await _enc_resolve_relation_prompts(
        ctx,
        source_value=source_value,
        target_app_token=target_app_token,
        target_table_id=target_table_id,
        target_table_key=target_table_key,
        target_match_field=target_match_field,
        prompt_fields=prompt_fields,
        join_with=join_with,
        max_items=max_items,
        strict=strict,
    )


def _normalize_relation_field_value(value: Any) -> str:
    items = _collect_display_strings(value)
    if not items:
        return ""
    if len(items) == 1:
        return str(items[0] or "").strip()
    return "，".join([str(x or "").strip() for x in items if str(x or "").strip()])


def _build_relation_prompt_from_fields(fields: dict[str, Any], prompt_fields: list[str], join_with: str) -> str:
    if not prompt_fields:
        return ""
    parts: list[str] = []
    for fn in prompt_fields:
        v = fields.get(fn) if fn else None
        ss = _collect_display_strings(v)
        if ss:
            parts.append(ss[0])
    j = str(join_with if join_with is not None else "\n")
    return j.join([x for x in parts if str(x).strip()]).strip()


async def _resolve_relation_param_items(
    ctx: AppContext,
    *,
    source_value: Any,
    target_app_token: str | None,
    target_table_id: str | None,
    target_table_key: str | None,
    target_match_field: str | None,
    item_param_map: dict[str, str],
    prompt_fields: list[str] | None,
    join_with: str,
    prompt_param: str | None,
    max_items: int,
    strict: bool,
) -> list[dict[str, Any]]:
    return await _enc_resolve_relation_param_items(
        ctx,
        source_value=source_value,
        target_app_token=target_app_token,
        target_table_id=target_table_id,
        target_table_key=target_table_key,
        target_match_field=target_match_field,
        item_param_map=item_param_map,
        prompt_fields=prompt_fields,
        join_with=join_with,
        prompt_param=prompt_param,
        max_items=max_items,
        strict=strict,
    )
def _runninghub_node_file_value(file_name: str) -> str:
    s = str(file_name or "").strip()
    if not s:
        return ""
    return s.lstrip("/")


def _apply_param_aliases(wf: WorkflowSpec, values: dict[str, Any]) -> dict[str, Any]:
    out = dict(values or {})
    aliases = (
        ("image", "images"),
        ("images", "image"),
        ("img", "image"),
        ("imgs", "images"),
    )
    for src, dst in aliases:
        if src in out and dst not in out and dst in (wf.params or {}):
            out[dst] = out.get(src)
    return out


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
        # 飞书返回的附件对象包含 url 字段，格式为完整下载链接（含 extra 参数，用于高级权限多维表格）
        attachment_url = it.get("url") if isinstance(it.get("url"), str) and str(it.get("url")).strip() else None
        download_dir = ctx.settings.temp_download_dir if provider == "runninghub" else (ctx.settings.temp_download_dir if ctx.settings.comfyui_upload_enabled else (ctx.settings.comfyui_input_dir or ctx.settings.temp_download_dir))
        try:
            saved = await ctx.drive.download_media(file_token=str(token), download_dir=download_dir, file_name=str(file_name) if file_name else None, download_url=attachment_url)
        except TypeError:
            # 旧版 drive.pyd 不支持 download_url 参数，降级调用
            saved = await ctx.drive.download_media(file_token=str(token), download_dir=download_dir, file_name=str(file_name) if file_name else None)
        if provider == "runninghub":
            if not runninghub:
                continue
            uploaded = await runninghub.upload_media_binary(file_path=saved)
            fv = _runninghub_node_file_value(uploaded.file_name or "")
            if fv:
                out.append(fv)
            try:
                if os.path.exists(saved) and ctx.settings.temp_download_dir in os.path.abspath(saved):
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
                if os.path.exists(saved) and ctx.settings.temp_download_dir in os.path.abspath(saved):
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
    im_cli = IMClient(ctx.auth)
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
        items = await im_cli.list_chat_messages(chat_id=cid, page_size=20)
        found: list[dict[str, Any]] = []
        msg_type_counts: dict[str, int] = {}
        scanned = 0
        for it0 in items:
            scanned += 1
            body = it0.get("body") if isinstance(it0.get("body"), dict) else {}
            mt = body.get("msg_type") if isinstance(body.get("msg_type"), str) else it0.get("msg_type")
            mt0 = str(mt or "").strip().lower() or "unknown"
            msg_type_counts[mt0] = int(msg_type_counts.get(mt0) or 0) + 1
            mid = it0.get("message_id") if isinstance(it0.get("message_id"), str) else None
            content_raw = body.get("content") if isinstance(body.get("content"), str) else it0.get("content")
            if not isinstance(content_raw, str) or not content_raw:
                continue
            try:
                obj = json.loads(content_raw)
            except Exception:
                continue
            if mt0 == "image":
                img_vals = _collect_values_by_key(obj, {"image_key", "imageKey"}, limit=5)
                for v0 in img_vals:
                    if isinstance(v0, str) and v0.strip():
                        found.append({"kind": "image", "key": v0.strip(), "message_id": mid})
            elif mt0 == "file":
                file_vals = _collect_values_by_key(obj, {"file_key", "fileKey"}, limit=5)
                for v1 in file_vals:
                    if isinstance(v1, str) and v1.strip():
                        found.append({"kind": "file", "key": v1.strip(), "message_id": mid})
            else:
                img_vals = _collect_values_by_key(obj, {"image_key", "imageKey"}, limit=5)
                for v0 in img_vals:
                    if isinstance(v0, str) and v0.strip():
                        found.append({"kind": "image", "key": v0.strip(), "message_id": mid})
                file_vals = _collect_values_by_key(obj, {"file_key", "fileKey"}, limit=5)
                for v1 in file_vals:
                    if isinstance(v1, str) and v1.strip():
                        found.append({"kind": "file", "key": v1.strip(), "message_id": mid})
            if len(found) >= nth:
                break
        if not found or nth > len(found):
            try:
                logging.warning(
                    "msg:last not found (chat_id=%s user=%s selector=%s). fetched=%d scanned=%d types=%s",
                    str(trigger.chat_id or ""),
                    str(trigger.user_open_id or ""),
                    sel0,
                    len(items),
                    scanned,
                    msg_type_counts,
                )
            except Exception:
                pass
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
                    raise RuntimeError("no recent attachment found for @msg:last (1) please send an image/file in this chat first; (2) if the bot cannot receive non-@ messages in group chats, add @bot when sending the attachment; (3) if the bot cannot see the attachment event, grant it message read permission; (4) fallback only scans recent messages; see console 'msg:last not found ... types=...' for what the bot can see)")
                akey = str(info.get("key") or "").strip()
                akind = str(info.get("kind") or "").strip().lower()
                fname = str(info.get("file_name") or "").strip()
                amid = str(info.get("message_id") or "").strip() or None
                ext = Path(fname).suffix if fname else (".png" if akind == "image" else ".bin")
                base_dir = str(ctx.settings.temp_download_dir or "").strip() or os.getcwd()
                tmp_dir = os.path.join(base_dir, "_im_attachments")
                os.makedirs(tmp_dir, exist_ok=True)
                tmp_path = os.path.join(tmp_dir, f"im_{akey[:12]}{ext}")
                if akind == "image":
                    await im_cli.download_image(image_key=akey, save_path=tmp_path, message_id=amid)
                else:
                    await im_cli.download_file(file_key=akey, save_path=tmp_path, message_id=amid)
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
                    raise RuntimeError("no recent attachment found for @msg:last (1) please send an image/file in this chat first; (2) if the bot cannot receive non-@ messages in group chats, add @bot when sending the attachment; (3) if the bot cannot see the attachment event, grant it message read permission; (4) fallback only scans recent messages; see console 'msg:last not found ... types=...' for what the bot can see)")
                akey = str(info.get("key") or "").strip()
                akind = str(info.get("kind") or "").strip().lower()
                fname = str(info.get("file_name") or "").strip()
                amid = str(info.get("message_id") or "").strip() or None
                ext = Path(fname).suffix if fname else (".png" if akind == "image" else ".bin")
                base_dir = str(ctx.settings.temp_download_dir or "").strip() or os.getcwd()
                tmp_dir = os.path.join(base_dir, "_im_attachments")
                os.makedirs(tmp_dir, exist_ok=True)
                tmp_path = os.path.join(tmp_dir, f"im_{akey[:12]}{ext}")
                if akind == "image":
                    await im_cli.download_image(image_key=akey, save_path=tmp_path, message_id=amid)
                else:
                    await im_cli.download_file(file_key=akey, save_path=tmp_path, message_id=amid)
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


def _map_fields_by_config(cfg: Any, values: dict[str, Any]) -> dict[str, Any]:
    return _enc_map_fields_by_config(cfg, values)


async def _bitable_create_record(*, auth: Any, app_token: str, table_id: str, fields: dict[str, Any]) -> str:
    return await _enc_bitable_create_record(auth=auth, app_token=app_token, table_id=table_id, fields=fields)


async def _mark_status(bitable: Any, record_id: str, status_key: str) -> None:
    await _enc_mark_status(bitable, record_id, status_key)


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
    return await _enc_reset_table_records(bitable, scope=scope, clear=clear)


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
    allow_default_table_fallback: bool = True,
) -> str | None:
    cfg_for_wf = (ctx.config.get("workflows") or {}).get(workflow_key) or {}
    resolved_table_key = table_key
    if not table_key and isinstance(cfg_for_wf, dict):
        tk = cfg_for_wf.get("table")
        if isinstance(tk, str) and tk:
            resolved_table_key = tk
    if not resolved_table_key and allow_default_table_fallback:
        resolved_table_key = ctx.default_table_key

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
            wf_fields0 = cfg_for_wf.get("writeBackFields") if isinstance(cfg_for_wf, dict) else None
            allow_write_error0 = True if not isinstance(wf_fields0, dict) else ("error" in wf_fields0)
            error_field = table_cfg.fields.get("error")
            status_field = table_cfg.fields.get("status")
            failed_value = table_cfg.status_values.get("failed")
            updates: dict[str, Any] = {}
            if allow_write_error0 and error_field:
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
    relation_split_items: list[dict[str, Any]] = []
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
    wf_write_back_fields = raw_cfg.get("writeBackFields")
    allow_write_prompt_id = True
    allow_write_error = True
    if isinstance(wf_write_back_fields, dict):
        allow_write_prompt_id = "prompt_id" in wf_write_back_fields
        allow_write_error = "error" in wf_write_back_fields
    run_log_table_key = raw_cfg.get("runLogTable") or raw_cfg.get("run_log_table") or raw_cfg.get("runLogTableKey") or raw_cfg.get("run_log_table_key")
    run_log_table_key = str(run_log_table_key).strip() if isinstance(run_log_table_key, str) and str(run_log_table_key).strip() else None
    runlog_bitable = ctx.bitables.get(run_log_table_key) if run_log_table_key else None
    runlog_cfg = ctx.bitable_configs.get(run_log_table_key) if run_log_table_key else None
    raw_defaults = raw_cfg.get("defaults") or {}
    if isinstance(raw_defaults, dict):
        merged.update(raw_defaults)
    split_param: str | None = None
    if use_record_fields:
        record_field_map = raw_cfg.get("recordFields") or {}
        relation_prompt = raw_cfg.get("relationPrompt") or raw_cfg.get("relation_prompt")
        if isinstance(relation_prompt, dict):
            src_field = relation_prompt.get("sourceField") or relation_prompt.get("source_field")
            src_field = str(src_field).strip() if isinstance(src_field, str) and str(src_field).strip() else None
            prompt_param = relation_prompt.get("targetParam") or relation_prompt.get("target_param") or "prompt"
            prompt_param = str(prompt_param).strip() if isinstance(prompt_param, str) and str(prompt_param).strip() else "prompt"
            tgt_key = relation_prompt.get("targetTableKey") or relation_prompt.get("target_table_key")
            tgt_key = str(tgt_key).strip() if isinstance(tgt_key, str) and str(tgt_key).strip() else None
            tgt_app = relation_prompt.get("targetAppToken") or relation_prompt.get("target_app_token") or relation_prompt.get("app_token")
            tgt_app = str(tgt_app).strip() if isinstance(tgt_app, str) and str(tgt_app).strip() else None
            tgt_tid = relation_prompt.get("targetTableId") or relation_prompt.get("target_table_id") or relation_prompt.get("table_id")
            tgt_tid = str(tgt_tid).strip() if isinstance(tgt_tid, str) and str(tgt_tid).strip() else None
            tgt_match = relation_prompt.get("targetMatchField") or relation_prompt.get("target_match_field")
            tgt_match = str(tgt_match).strip() if isinstance(tgt_match, str) and str(tgt_match).strip() else None
            ipm_raw = relation_prompt.get("itemParamMap") or relation_prompt.get("item_param_map") or relation_prompt.get("item_params") or {}
            item_param_map: dict[str, str] = {}
            if isinstance(ipm_raw, dict):
                for k, v in ipm_raw.items():
                    kk = str(k or "").strip()
                    vv = str(v or "").strip()
                    if kk and vv:
                        item_param_map[kk] = vv
            pf = relation_prompt.get("promptFields") or relation_prompt.get("prompt_fields") or []
            prompt_fields: list[str] = []
            if isinstance(pf, list):
                for x in pf:
                    if isinstance(x, str) and x.strip():
                        prompt_fields.append(x.strip())
            elif isinstance(pf, str) and pf.strip():
                prompt_fields = [pf.strip()]
            join_with = relation_prompt.get("joinWith") or relation_prompt.get("join_with") or "\n"
            join_with = str(join_with) if isinstance(join_with, str) else "\n"
            max_items = relation_prompt.get("maxItems") or relation_prompt.get("max_items") or 20
            max_items = int(max_items) if isinstance(max_items, int) else (int(str(max_items)) if str(max_items).strip().isdigit() else 20)
            max_items = max(1, min(100, max_items))
            enable_split = relation_prompt.get("split")
            enable_split = True if enable_split is None else bool(enable_split)
            strict = relation_prompt.get("strict")
            strict = True if strict is None else bool(strict)
            enable_item_param_map = relation_prompt.get("enableItemParamMap") if isinstance(relation_prompt.get("enableItemParamMap"), bool) else relation_prompt.get("enable_item_param_map")
            enable_item_param_map = True if enable_item_param_map is None else bool(enable_item_param_map)
            enable_prompt_fields = relation_prompt.get("enablePromptFields") if isinstance(relation_prompt.get("enablePromptFields"), bool) else relation_prompt.get("enable_prompt_fields")
            enable_prompt_fields = True if enable_prompt_fields is None else bool(enable_prompt_fields)
            has_item_targets = enable_item_param_map and bool(item_param_map) and any((k in wf.params) for k in item_param_map.keys())
            if src_field and has_item_targets:
                src_val = record_fields.get(src_field)
                related_items = await _resolve_relation_param_items(
                    ctx,
                    source_value=src_val,
                    target_app_token=tgt_app,
                    target_table_id=tgt_tid,
                    target_table_key=tgt_key,
                    target_match_field=tgt_match,
                    item_param_map=item_param_map,
                    prompt_fields=prompt_fields if (enable_prompt_fields and prompt_fields) else None,
                    join_with=join_with,
                    prompt_param=prompt_param,
                    max_items=max_items,
                    strict=strict,
                )
                if not related_items:
                    if strict:
                        raise RuntimeError(
                            "relationPrompt 未匹配到任何关联记录：请检查表A的选择值/record_id 是否能在表B中找到，以及 itemParamMap 的字段名是否存在。"
                        )
                else:
                    if enable_split:
                        relation_split_items = related_items
                        split_param = split_param or prompt_param
                    else:
                        merged.update(related_items[0])
            elif src_field and enable_prompt_fields and prompt_fields:
                src_val = record_fields.get(src_field)
                related_prompts = await _resolve_relation_prompts(
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
                if not related_prompts:
                    if strict:
                        raise RuntimeError(
                            "relationPrompt 未匹配到任何关联提示词：请检查表A的“选择屏数”值是否能在表B的匹配列中找到（例如匹配列=屏类型），以及表B是否存在要拼接的字段（通用总控提示词/专用生图提示词）。"
                        )
                else:
                    merged[prompt_param] = related_prompts
                    if enable_split:
                        split_param = split_param or prompt_param
            elif src_field and enable_item_param_map and item_param_map and not has_item_targets and strict and not (enable_prompt_fields and prompt_fields):
                raise RuntimeError("relationPrompt 配置了 itemParamMap，但工作流 params 里没有对应的参数映射：请先在 params 中新增这些参数并配置 targets，或改用 promptFields 拼接。")
        for param_key in wf.params.keys():
            field_name = record_field_map.get(param_key) or param_key
            if field_name in record_fields:
                raw_val = record_fields.get(field_name)
                try:
                    downloaded = await _download_attachments(
                        ctx,
                        provider=provider,
                        comfyui=comfyui_client,
                        runninghub=runninghub_client,
                        value=raw_val,
                    )
                except Exception as e:
                    err_msg = f"下载附件失败(param={param_key}): {e}"
                    logging.exception("download_attachments failed for record=%s param=%s", record_id, param_key)
                    if record_id and not record_id.startswith("mock_rec_") and bitable and table_cfg and bitable.mode.write_enabled:
                        error_field = table_cfg.fields.get("error")
                        status_field = table_cfg.fields.get("status")
                        failed_value = table_cfg.status_values.get("failed")
                        updates: dict[str, Any] = {}
                        if allow_write_error and (not split_active) and error_field:
                            updates[error_field] = err_msg
                        if status_field and failed_value:
                            updates[status_field] = failed_value
                        if updates:
                            try:
                                await bitable.update_record(record_id, updates)
                            except Exception:
                                pass
                    raise RuntimeError(err_msg) from e
                v = downloaded if downloaded else raw_val
                v = _normalize_bitable_value_for_param(v, wf, param_key)
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
    merged = _apply_param_aliases(wf, merged)

    if not split_param:
        relation_prompt = raw_cfg.get("relationPrompt") or raw_cfg.get("relation_prompt")
        if isinstance(relation_prompt, dict):
            enable_split = relation_prompt.get("split")
            enable_split = True if enable_split is None else bool(enable_split)
            prompt_param = relation_prompt.get("targetParam") or relation_prompt.get("target_param") or "prompt"
            prompt_param = str(prompt_param).strip() if isinstance(prompt_param, str) and str(prompt_param).strip() else "prompt"
            v = merged.get(prompt_param)
            if enable_split and isinstance(v, list) and len(v) > 1:
                split_param = prompt_param
    split_max = 50

    temp_files: list[str] = []
    base_download_dir = (ctx.settings.temp_download_dir or "").strip()
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

    split_values: list[str] = []
    split_items: list[dict[str, Any]] = relation_split_items[:split_max] if relation_split_items else []
    if not split_items and split_param and split_param in merged and split_param not in params:
        v0 = merged.get(split_param)
        if isinstance(v0, list):
            split_values = [str(x).strip() for x in v0 if str(x).strip()][:split_max]

    base_cb_ctx: dict[str, Any] = {
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
    }
    split_group = None
    if (split_values or split_items) and record_id:
        split_group = f"{record_id}_{int(time.time() * 1000)}"
    split_active = bool(split_values or split_items)
    run_group = split_group
    if not run_group and record_id:
        run_group = f"{record_id}_{int(time.time() * 1000)}"

    prompt_id: str | None = None
    err_msg: str | None = None
    prompt_ids: list[str] = []
    prompt_parts: list[str] | None = None
    planned_total = 1
    try:
        runs: list[Any] = split_items if split_items else (split_values if split_values else [None])
        planned_total = len(runs) if runs else 1
        if (
            record_id
            and not record_id.startswith("mock_rec_")
            and getattr(ctx, "runner", None)
            and hasattr(ctx.runner, "register_record_run")
        ):
            try:
                await ctx.runner.register_record_run(record_id=record_id, table_key=resolved_table_key, workflow_key=workflow_key, planned_total=planned_total)
            except Exception:
                pass
        for idx, sp in enumerate(runs):
            run_log_record_id: str | None = None
            run_log_submitted_at_ms: int | None = None
            merged_one = dict(merged)
            if isinstance(sp, dict):
                merged_one.update(sp)
            elif sp is not None and split_param:
                merged_one[split_param] = sp

            node_info_list = ctx.workflows.build_node_info_list(wf, merged_one)
            inline_node_info_list = _build_inline_node_info_list(params)
            if inline_node_info_list:
                node_info_list = node_info_list + inline_node_info_list

            cb_ctx = dict(base_cb_ctx)
            cb_ctx["run_group"] = run_group
            cb_ctx["table_key_for_output"] = resolved_table_key
            cb_ctx["workflow_key_for_output"] = workflow_key
            if split_values or split_items:
                cb_ctx["split_group"] = split_group
                cb_ctx["split_total"] = len(runs)
                cb_ctx["split_index"] = idx
                cb_ctx["append_output"] = True
                if isinstance(sp, dict) and isinstance(sp.get("__relation_record_id"), str) and str(sp.get("__relation_record_id")).strip():
                    cb_ctx["relation_record_id"] = str(sp.get("__relation_record_id")).strip()
            extra_data = {"callback_url": _pick_callback_url_for_base(ctx, comfyui_base_url), "callback_context": cb_ctx}

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
                if not prompt_id:
                    logging.warning("runninghub create_task succeeded but task_id is missing: workflow=%s", workflow_key)
                    err_msg = "RunningHub 创建任务成功但未返回 task_id，无法跟踪任务状态"
                if ctx.settings.save_task_request_params:
                    try:
                        save_dir = ctx.settings.temp_download_dir
                        os.makedirs(save_dir, exist_ok=True)
                        dump: dict[str, Any] = {
                            "provider": provider,
                            "workflow_key": workflow_key,
                            "workflow_name": getattr(wf, "workflow_name", workflow_key),
                            "task_id": prompt_id,
                            "runninghub_workflow_id": workflow_id,
                            "node_info_list": node_info_list,
                        }
                        ts = int(time.time() * 1000)
                        wf_slug = str(workflow_key).replace("/", "_").replace("\\", "_")[:60]
                        pid_short = str(prompt_id or "noid")[:24].replace("/", "_").replace("\\", "_")
                        dump_path = os.path.join(save_dir, f"submit_{wf_slug}_{pid_short}_{ts}.json")
                        with open(dump_path, "w", encoding="utf-8") as f:
                            json.dump(dump, f, ensure_ascii=False, indent=2, default=str)
                        logging.info("Submission dump written to %s", dump_path)
                    except Exception:
                        pass
            else:
                prompt_id = await queue_by_workflowprompt(
                    comfyui_client,
                    wf=wf,
                    node_info_list=node_info_list,
                    extra_data=extra_data,
                )
                if not prompt_id and not err_msg:
                    err_msg = "ComfyUI 提交任务成功但未返回 prompt_id，无法跟踪任务状态"
                if ctx.settings.save_task_request_params:
                    try:
                        save_dir = ctx.settings.temp_download_dir
                        os.makedirs(save_dir, exist_ok=True)
                        dump: dict[str, Any] = {
                            "provider": provider,
                            "workflow_key": workflow_key,
                            "workflow_name": getattr(wf, "workflow_name", workflow_key),
                            "prompt_id": prompt_id,
                            "comfyui_base_url": comfyui_base_url,
                            "node_info_list": node_info_list,
                            "extra_data": extra_data,
                        }
                        api_path = getattr(wf, "api_workflow_path", None)
                        if api_path:
                            dump["api_workflow_path"] = api_path
                            try:
                                dump["api_workflow_json"] = json.loads(Path(api_path).read_text(encoding="utf-8"))
                            except Exception:
                                dump["api_workflow_json"] = "(failed to read)"
                        ts = int(time.time() * 1000)
                        wf_slug = str(workflow_key).replace("/", "_").replace("\\", "_")[:60]
                        pid_short = str(prompt_id or "noid")[:24].replace("/", "_").replace("\\", "_")
                        dump_path = os.path.join(save_dir, f"submit_{wf_slug}_{pid_short}_{ts}.json")
                        with open(dump_path, "w", encoding="utf-8") as f:
                            json.dump(dump, f, ensure_ascii=False, indent=2, default=str)
                        logging.info("Submission dump written to %s", dump_path)
                    except Exception:
                        pass

            if prompt_id:
                prompt_ids.append(prompt_id)
                if (
                    runlog_bitable
                    and runlog_cfg
                    and getattr(runlog_bitable, "mode", None)
                    and getattr(runlog_bitable.mode, "write_enabled", False)
                    and record_id
                    and run_group
                ):
                    try:
                        run_log_submitted_at_ms = int(time.time() * 1000)
                        raw_fields = {
                            "source_record_id": record_id,
                            "source_table_key": resolved_table_key,
                            "workflow_key": workflow_key,
                            "workflow_name": wf.workflow_name if wf else workflow_key,
                            "run_group": run_group,
                            "run_total": len(runs),
                            "run_index": idx,
                            "provider": provider,
                            "task_id": prompt_id,
                            "task_status": "已提交",
                            "submitted_at": run_log_submitted_at_ms,
                        }
                        fields = _map_fields_by_config(runlog_cfg, raw_fields)
                        if fields:
                            run_log_record_id = await _bitable_create_record(auth=ctx.auth, app_token=runlog_cfg.app_token, table_id=runlog_cfg.table_id, fields=fields)
                    except Exception:
                        run_log_record_id = None
                if run_log_table_key and run_log_record_id and isinstance(cb_ctx, dict):
                    cb_ctx["runLogTableKey"] = run_log_table_key
                    cb_ctx["runLogRecordId"] = run_log_record_id
                    if isinstance(run_log_submitted_at_ms, int) and run_log_submitted_at_ms > 0:
                        cb_ctx["runLogSubmittedAtMs"] = run_log_submitted_at_ms
                if record_id and not record_id.startswith("mock_rec_") and bitable and table_cfg and bitable.mode.write_enabled:
                    updates: dict[str, Any] = {}
                    status_field = table_cfg.fields.get("status")
                    running_value = table_cfg.status_values.get("running")
                    if status_field and running_value:
                        updates[status_field] = running_value
                    prompt_field = table_cfg.fields.get("prompt_id")
                    if allow_write_prompt_id and (not split_active) and prompt_field:
                        if prompt_parts is None:
                            cur = record_fields.get(prompt_field)
                            cur_s = str(cur).strip() if isinstance(cur, str) else ""
                            prompt_parts = [x.strip() for x in cur_s.splitlines() if x.strip()] if cur_s else []
                        if prompt_id and prompt_id not in prompt_parts:
                            prompt_parts.append(prompt_id)
                        updates[prompt_field] = "\n".join(prompt_parts) if prompt_parts else ""
                        record_fields[prompt_field] = updates[prompt_field]
                    if updates:
                        try:
                            await bitable.update_record(record_id, updates)
                        except Exception as e:
                            logging.warning(
                                "回写飞书失败(提交阶段): table=%s record=%s updates=%s err=%s",
                                resolved_table_key,
                                record_id,
                                list(updates.keys()),
                                str(e),
                            )

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
                            user_open_id=trigger.user_open_id,
                            run_log_table_key=run_log_table_key,
                            run_log_record_id=run_log_record_id,
                            run_log_submitted_at_ms=run_log_submitted_at_ms,
                            split_group=split_group,
                            split_total=len(runs) if (split_values or split_items) else None,
                            split_index=idx if (split_values or split_items) else None,
                            append_output=True if (split_values or split_items) else None,
                        )
                    except Exception:
                        pass
                if hasattr(ctx.runner, "register_pending_remote"):
                    try:
                        if provider == "runninghub" or (provider != "runninghub" and not _is_local_base_url(comfyui_base_url)):
                            await ctx.runner.register_pending_remote(prompt_id=prompt_id, provider=provider, comfyui_base_url=comfyui_base_url)
                    except Exception:
                        pass

        if prompt_ids:
            prompt_id = prompt_ids[-1]
    except httpx.HTTPStatusError as e:
        status = e.response.status_code if e.response else None
        body_text = ""
        if e.response is not None:
            try:
                ct = str(e.response.headers.get("content-type") or "").lower()
                if "application/json" in ct:
                    body_text = json.dumps(e.response.json(), ensure_ascii=False)
                else:
                    body_text = e.response.text
            except Exception:
                try:
                    body_text = e.response.text
                except Exception:
                    body_text = ""
        body_short = body_text.strip()
        if len(body_short) > 800:
            body_short = body_short[:800] + "..."
        # 将请求参数写入 temp 目录，方便排查
        try:
            save_dir = ctx.settings.temp_download_dir
            os.makedirs(save_dir, exist_ok=True)
            dump: dict[str, Any] = {
                "provider": provider,
                "workflow_key": workflow_key,
                "workflow_name": getattr(wf, "workflow_name", workflow_key),
                "http_status": status,
                "response_body": body_text.strip()[:2000],
            }
            if provider != "runninghub":
                dump["node_info_list"] = node_info_list
                dump["extra_data"] = extra_data
                api_path = getattr(wf, "api_workflow_path", None)
                if api_path:
                    dump["api_workflow_path"] = api_path
                    try:
                        dump["api_workflow_json"] = json.loads(Path(api_path).read_text(encoding="utf-8"))
                    except Exception:
                        dump["api_workflow_json"] = "(failed to read)"
            else:
                dump["runninghub_workflow_id"] = cfg_for_wf.get("runninghubWorkflowId") if isinstance(cfg_for_wf, dict) else None
            ts = int(time.time() * 1000)
            wf_slug = str(workflow_key).replace("/", "_").replace("\\", "_")[:60]
            dump_path = os.path.join(save_dir, f"comfyui_error_{wf_slug}_{ts}.json")
            with open(dump_path, "w", encoding="utf-8") as f:
                json.dump(dump, f, ensure_ascii=False, indent=2, default=str)
            logging.info("Request debug dump written to %s", dump_path)
        except Exception:
            logging.exception("Failed to write request debug dump")
        if provider == "runninghub":
            err_msg = f"HTTPError {status if status is not None else 'unknown'}: {e}"
        else:
            if status in (400, 404, 422):
                user_msg, allow_fallback = _build_workflowprompt_user_error(
                    workflow_name=getattr(wf, "workflow_name", workflow_key),
                    status_code=status,
                    body_text=body_text,
                )
                logging.warning(
                    "workflowprompt failed: workflow=%s status=%s allow_fallback=%s response=%s",
                    getattr(wf, "workflow_name", workflow_key),
                    status,
                    allow_fallback,
                    body_short,
                )
                if not allow_fallback:
                    err_msg = user_msg
                elif not wf.api_workflow_path:
                    err_msg = (
                        user_msg
                        + "\n但当前 workflow 没有配置 `apiWorkflowPath`，所以无法继续降级执行。"
                    )
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
                        res = await comfyui_client.queue_api_prompt(prompt=prompt, extra_data=extra_data)
                        prompt_id = res.prompt_id
                        err_msg = None
                    except Exception as e2:
                        err_msg = (
                            user_msg
                            + f"\n随后尝试 `apiWorkflowPath` 降级执行也失败了：{e2}"
                        )
            else:
                err_msg = f"HTTPError {status if status is not None else 'unknown'}: {e}" + (f"\nresponse={body_short}" if body_short else "")
    except Exception as e:
        err_msg = str(e)

    if (
        record_id
        and not record_id.startswith("mock_rec_")
        and getattr(ctx, "runner", None)
        and hasattr(ctx.runner, "finalize_record_run")
    ):
        try:
            await ctx.runner.finalize_record_run(
                record_id=record_id,
                table_key=resolved_table_key,
                workflow_key=workflow_key,
                submitted_total=len(prompt_ids),
            )
        except Exception:
            pass

    if err_msg:
        if record_id and not record_id.startswith("mock_rec_") and bitable and table_cfg and bitable.mode.write_enabled:
            error_field = table_cfg.fields.get("error")
            status_field = table_cfg.fields.get("status")
            failed_value = table_cfg.status_values.get("failed")
            updates: dict[str, Any] = {}
            if allow_write_error and (not split_active) and error_field:
                updates[error_field] = err_msg
            if status_field and failed_value:
                updates[status_field] = failed_value
            if updates:
                try:
                    await bitable.update_record(record_id, updates)
                except Exception:
                    pass
        if (
            runlog_bitable
            and runlog_cfg
            and getattr(runlog_bitable, "mode", None)
            and getattr(runlog_bitable.mode, "write_enabled", False)
            and record_id
            and run_group
        ):
            try:
                now_ms = int(time.time() * 1000)
                raw_fields = {
                    "source_record_id": record_id,
                    "source_table_key": resolved_table_key,
                    "workflow_key": workflow_key,
                    "workflow_name": wf.workflow_name if wf else workflow_key,
                    "run_group": run_group,
                    "run_total": planned_total,
                    "run_index": 0,
                    "provider": provider,
                    "task_id": "",
                    "task_status": "失败",
                    "submitted_at": now_ms,
                    "finished_at": now_ms,
                    "duration_sec": 0,
                    "error_message": err_msg,
                }
                fields = _map_fields_by_config(runlog_cfg, raw_fields)
                if fields:
                    await _bitable_create_record(auth=ctx.auth, app_token=runlog_cfg.app_token, table_id=runlog_cfg.table_id, fields=fields)
            except Exception:
                pass
        raise RuntimeError(err_msg)

    if (prompt_id or prompt_ids) and record_id and not record_id.startswith("mock_rec_") and bitable and table_cfg and bitable.mode.write_enabled:
        updates: dict[str, Any] = {}
        status_field = table_cfg.fields.get("status")
        running_value = table_cfg.status_values.get("running")
        if status_field and running_value:
            updates[status_field] = running_value
        prompt_field = table_cfg.fields.get("prompt_id")
        if allow_write_prompt_id and (not split_active) and prompt_field:
            if prompt_ids:
                cur = record_fields.get(prompt_field)
                cur_s = str(cur).strip() if isinstance(cur, str) else ""
                parts = [x.strip() for x in cur_s.splitlines() if x.strip()] if cur_s else []
                for pid in prompt_ids:
                    if pid and pid not in parts:
                        parts.append(pid)
                updates[prompt_field] = "\n".join(parts) if parts else ""
            else:
                updates[prompt_field] = prompt_id
        if updates:
            try:
                await bitable.update_record(record_id, updates)
            except Exception as e:
                logging.warning("回写飞书失败: table=%s record=%s updates=%s err=%s", resolved_table_key, record_id, list(updates.keys()), str(e))
                if trigger.chat_id:
                    try:
                        await IMClient(ctx.auth).send_text(chat_id=trigger.chat_id, text=f"回写飞书失败（所以任务状态/任务ID可能不会回填）。错误：{e}")
                    except Exception:
                        pass
    return prompt_id


async def preview_workflow_runs(
    ctx: AppContext,
    *,
    trigger: TriggerContext,
    workflow_key: str,
    record_id: str | None,
    row: int | None,
    view_id: str | None,
    params: dict[str, Any],
    table_key: str | None,
    resolve_files: bool,
) -> dict[str, Any]:
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
        raise RuntimeError(f"未找到工作流配置: {workflow_key}")

    merged: dict[str, Any] = {}
    relation_split_items: list[dict[str, Any]] = []
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

    split_param: str | None = None
    if use_record_fields:
        record_field_map = raw_cfg.get("recordFields") or {}

        relation_prompt = raw_cfg.get("relationPrompt") or raw_cfg.get("relation_prompt")
        if isinstance(relation_prompt, dict):
            src_field = relation_prompt.get("sourceField") or relation_prompt.get("source_field")
            src_field = str(src_field).strip() if isinstance(src_field, str) and str(src_field).strip() else None
            prompt_param = relation_prompt.get("targetParam") or relation_prompt.get("target_param") or "prompt"
            prompt_param = str(prompt_param).strip() if isinstance(prompt_param, str) and str(prompt_param).strip() else "prompt"
            tgt_key = relation_prompt.get("targetTableKey") or relation_prompt.get("target_table_key")
            tgt_key = str(tgt_key).strip() if isinstance(tgt_key, str) and str(tgt_key).strip() else None
            tgt_app = relation_prompt.get("targetAppToken") or relation_prompt.get("target_app_token") or relation_prompt.get("app_token")
            tgt_app = str(tgt_app).strip() if isinstance(tgt_app, str) and str(tgt_app).strip() else None
            tgt_tid = relation_prompt.get("targetTableId") or relation_prompt.get("target_table_id") or relation_prompt.get("table_id")
            tgt_tid = str(tgt_tid).strip() if isinstance(tgt_tid, str) and str(tgt_tid).strip() else None
            tgt_match = relation_prompt.get("targetMatchField") or relation_prompt.get("target_match_field")
            tgt_match = str(tgt_match).strip() if isinstance(tgt_match, str) and str(tgt_match).strip() else None
            ipm_raw = relation_prompt.get("itemParamMap") or relation_prompt.get("item_param_map") or relation_prompt.get("item_params") or {}
            item_param_map: dict[str, str] = {}
            if isinstance(ipm_raw, dict):
                for k, v in ipm_raw.items():
                    kk = str(k or "").strip()
                    vv = str(v or "").strip()
                    if kk and vv:
                        item_param_map[kk] = vv
            pf = relation_prompt.get("promptFields") or relation_prompt.get("prompt_fields") or []
            prompt_fields: list[str] = []
            if isinstance(pf, list):
                for x in pf:
                    if isinstance(x, str) and x.strip():
                        prompt_fields.append(x.strip())
            elif isinstance(pf, str) and pf.strip():
                prompt_fields = [pf.strip()]
            join_with = relation_prompt.get("joinWith") or relation_prompt.get("join_with") or "\n"
            join_with = str(join_with) if isinstance(join_with, str) else "\n"
            max_items = relation_prompt.get("maxItems") or relation_prompt.get("max_items") or 20
            max_items = int(max_items) if isinstance(max_items, int) else (int(str(max_items)) if str(max_items).strip().isdigit() else 20)
            max_items = max(1, min(100, max_items))
            enable_split = relation_prompt.get("split")
            enable_split = True if enable_split is None else bool(enable_split)
            strict = relation_prompt.get("strict")
            strict = True if strict is None else bool(strict)
            enable_item_param_map = relation_prompt.get("enableItemParamMap") if isinstance(relation_prompt.get("enableItemParamMap"), bool) else relation_prompt.get("enable_item_param_map")
            enable_item_param_map = True if enable_item_param_map is None else bool(enable_item_param_map)
            enable_prompt_fields = relation_prompt.get("enablePromptFields") if isinstance(relation_prompt.get("enablePromptFields"), bool) else relation_prompt.get("enable_prompt_fields")
            enable_prompt_fields = True if enable_prompt_fields is None else bool(enable_prompt_fields)
            has_item_targets = enable_item_param_map and bool(item_param_map) and any((k in wf.params) for k in item_param_map.keys())
            if src_field and has_item_targets:
                src_val = record_fields.get(src_field)
                related_items = await _resolve_relation_param_items(
                    ctx,
                    source_value=src_val,
                    target_app_token=tgt_app,
                    target_table_id=tgt_tid,
                    target_table_key=tgt_key,
                    target_match_field=tgt_match,
                    item_param_map=item_param_map,
                    prompt_fields=prompt_fields if (enable_prompt_fields and prompt_fields) else None,
                    join_with=join_with,
                    prompt_param=prompt_param,
                    max_items=max_items,
                    strict=strict,
                )
                if not related_items:
                    if strict:
                        raise RuntimeError("relationPrompt 未匹配到任何关联记录：请检查表A的选择值/record_id 是否能在表B中找到，以及 itemParamMap 的字段名是否存在。")
                else:
                    if enable_split:
                        relation_split_items = related_items
                        split_param = split_param or prompt_param
                    else:
                        merged.update(related_items[0])
            elif src_field and enable_prompt_fields and prompt_fields:
                src_val = record_fields.get(src_field)
                related_prompts = await _resolve_relation_prompts(
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
                if not related_prompts:
                    if strict:
                        raise RuntimeError(
                            "relationPrompt 未匹配到任何关联提示词：请检查表A的关联字段值是否能在表B的匹配列中找到，以及表B是否存在要拼接的字段。"
                        )
                else:
                    merged[prompt_param] = related_prompts
                    if enable_split:
                        split_param = split_param or prompt_param
            elif src_field and enable_item_param_map and item_param_map and not has_item_targets and strict and not (enable_prompt_fields and prompt_fields):
                raise RuntimeError("relationPrompt 配置了 itemParamMap，但工作流 params 里没有对应的参数映射：请先在 params 中新增这些参数并配置 targets，或改用 promptFields 拼接。")

        for param_key in wf.params.keys():
            field_name = record_field_map.get(param_key) or param_key
            if field_name not in record_fields:
                continue
            raw_val = record_fields.get(field_name)
            v = raw_val
            if resolve_files:
                downloaded = await _download_attachments(
                    ctx,
                    provider=provider,
                    comfyui=comfyui_client,
                    runninghub=runninghub_client,
                    value=raw_val,
                )
                v = downloaded if downloaded else raw_val
            v = _normalize_bitable_value_for_param(v, wf, param_key)

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

    if resolve_files:
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
    merged = _apply_param_aliases(wf, merged)

    if not split_param:
        relation_prompt = raw_cfg.get("relationPrompt") or raw_cfg.get("relation_prompt")
        if isinstance(relation_prompt, dict):
            enable_split = relation_prompt.get("split")
            enable_split = True if enable_split is None else bool(enable_split)
            prompt_param = relation_prompt.get("targetParam") or relation_prompt.get("target_param") or "prompt"
            prompt_param = str(prompt_param).strip() if isinstance(prompt_param, str) and str(prompt_param).strip() else "prompt"
            v = merged.get(prompt_param)
            if enable_split and isinstance(v, list) and len(v) > 1:
                split_param = prompt_param

    split_max = 50

    split_values: list[str] = []
    split_items: list[dict[str, Any]] = relation_split_items[:split_max] if relation_split_items else []
    if not split_items and split_param and split_param in merged and split_param not in params:
        v0 = merged.get(split_param)
        if isinstance(v0, list):
            split_values = [str(x).strip() for x in v0 if str(x).strip()][:split_max]

    base_cb_ctx: dict[str, Any] = {
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
    }
    split_group = None
    if (split_values or split_items) and record_id:
        split_group = f"{record_id}_{int(time.time() * 1000)}"
    run_group = split_group
    if not run_group and record_id:
        run_group = f"{record_id}_{int(time.time() * 1000)}"

    runs: list[Any] = split_items if split_items else (split_values if split_values else [None])
    out_runs: list[dict[str, Any]] = []
    for idx, sp in enumerate(runs):
        merged_one = dict(merged)
        if isinstance(sp, dict):
            merged_one.update(sp)
        elif sp is not None and split_param:
            merged_one[split_param] = sp
        node_info_list = ctx.workflows.build_node_info_list(wf, merged_one)
        inline_node_info_list = _build_inline_node_info_list(params)
        if inline_node_info_list:
            node_info_list = node_info_list + inline_node_info_list
        cb_ctx = dict(base_cb_ctx)
        cb_ctx["run_group"] = run_group
        cb_ctx["table_key_for_output"] = resolved_table_key
        cb_ctx["workflow_key_for_output"] = workflow_key
        if split_values or split_items:
            cb_ctx["split_group"] = split_group
            cb_ctx["split_total"] = len(runs)
            cb_ctx["split_index"] = idx
            cb_ctx["append_output"] = True
            if isinstance(sp, dict) and isinstance(sp.get("__relation_record_id"), str) and str(sp.get("__relation_record_id")).strip():
                cb_ctx["relation_record_id"] = str(sp.get("__relation_record_id")).strip()
        extra_data = {"callback_url": _pick_callback_url_for_base(ctx, comfyui_base_url), "callback_context": cb_ctx}
        out_runs.append(
            {
                "index": idx,
                "param_key": split_param,
                "param_value": sp,
                "node_info_list": node_info_list,
                "extra_data": extra_data,
            }
        )

    return {
        "workflow": workflow_key,
        "table": resolved_table_key,
        "record_id": record_id,
        "provider": provider,
        "comfyui_base_url": comfyui_base_url,
        "runs": out_runs,
    }


async def dispatch(ctx: AppContext, *, name: str, args: dict[str, Any], trigger: TriggerContext) -> None:
    im = IMClient(ctx.auth)

    if name in ("help", "h"):
        await _send_text_by_trigger(im, trigger, get_help_text())
        return

    if name == "ids":
        chat_id = str(trigger.chat_id or "")
        user_open_id = str(trigger.user_open_id or "")
        await _send_text_by_trigger(im, trigger, f"chat_id={chat_id}\nuser_open_id={user_open_id}")
        return

    if name == "botid":
        token = ""
        try:
            token = await ctx.auth.tenant_token()
        except Exception as e:
            await _send_text_by_trigger(im, trigger, f"bot_id error: {e}")
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
            await _send_text_by_trigger(im, trigger, f"bot_id error: {e}")
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
            await _send_text_by_trigger(im, trigger, f"bot_open_id={bot_open_id}")
        else:
            await _send_text_by_trigger(im, trigger, f"bot_open_id not found: {data}")
        return

    if name == "cb":
        sig = str(args.get("sig") or args.get("token") or "").strip()
        if ctx.settings.cb_message_token and sig != ctx.settings.cb_message_token:
            if trigger.chat_id:
                await _send_text_by_trigger(im, trigger, "cb token mismatch")
            return
        payload: dict[str, Any] | None = None
        raw_data = str(args.get("data") or "").strip()
        if raw_data:
            payload = _b64url_json_decode(raw_data)
            if not payload:
                if trigger.chat_id:
                    await _send_text_by_trigger(im, trigger, "cb invalid data")
                return
        else:
            provider = str(args.get("provider") or args.get("p") or "comfyui").strip().lower()
            pid = args.get("id") or args.get("prompt_id") or args.get("promptId") or args.get("taskId")
            pid = str(pid).strip() if isinstance(pid, (str, int)) else ""
            if not pid:
                if trigger.chat_id:
                    await _send_text_by_trigger(im, trigger, "cb missing id")
                return
            payload = {"provider": provider, "prompt_id": pid, "completed": True}
            if provider == "runninghub":
                try:
                    cli = RunningHubClient(api_key=str(ctx.settings.runninghub_api_key or ""))
                    q = await cli.query_results_v2(task_id=pid)
                except Exception as e:
                    if trigger.chat_id:
                        await _send_text_by_trigger(im, trigger, f"cb runninghub query error: {e}")
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
                        await _send_text_by_trigger(im, trigger, f"cb still running: {pid}")
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
            await _send_text_by_trigger(im, trigger, f"cb received{(' prompt=' + prompt_id) if prompt_id else ''}")
        try:
            await handle_callback_payload(ctx, payload)
        except Exception as e:
            if trigger.chat_id:
                await _send_text_by_trigger(im, trigger, f"cb error: {e}")
        return

    if name == "panel":
        await _send_card_by_trigger(im, trigger, build_panel_card(ctx))
        return

    if name in ("reset", "reset_table"):
        table_key = _pick_table_key(args) or ctx.default_table_key
        bitable = ctx.bitables.get(table_key) if table_key else None
        if not bitable:
            if trigger.chat_id:
                if (not ctx.bitable_mode.read_enabled) and ctx.bitable_configs and (ctx.settings.bitable_mode or "").strip().lower() not in ("off", "none", "disable", "disabled"):
                    await _send_license_guidance(im, trigger.chat_id)
                else:
                    await _send_text_by_trigger(im, trigger, "缺少 table 或 table 不存在")
            return
        scope = str(args.get("scope") or "all_nonqueued").strip()
        clear = str(args.get("clear") or "").strip().lower() in ("1", "true", "yes", "y", "on")
        n, cp, ce, co, fc, sp, se, so = await _reset_table_records(bitable, scope=scope, clear=clear)
        if trigger.chat_id:
            if clear:
                await _send_text_by_trigger(im, trigger, f"已重置: {n} 清空(任务ID:{cp} 错误:{ce} 结果:{co} 失败:{fc} 不可写:任务ID:{sp} 错误:{se} 结果:{so})")
            else:
                await _send_text_by_trigger(im, trigger, f"已重置: {n}")
        return

    if name == "run_default":
        record_id: str | None = None
        default_table_key = ctx.default_table_key
        bitable = ctx.bitables.get(default_table_key) if default_table_key else None
        
        if bitable and bitable.mode.read_enabled:
            record_id = await bitable.find_next_queued_record_id()
            if not record_id:
                await _send_text_by_trigger(im, trigger, "队列为空：未找到 queued 任务")
                return
        workflow_key = _pick_default_workflow_key(ctx, table_key=default_table_key)
        if not workflow_key:
            await _send_text_by_trigger(im, trigger, "未找到默认工作流配置")
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
        await _send_text_by_trigger(im, trigger, f"已入队: {prompt_id or 'unknown'}")
        return

    if name in ("run", "wf"):
        record_id = _pick_record_id(args)
        row = _pick_row(args)
        view_id = _pick_view_id(args)
        explicit_table_key = _pick_table_key(args)
        workflow_arg = _pick_workflow_key(args)
        workflow_key = _pick_workflow_key(args) if name == "wf" else (args.get("workflow") or "default")
        workflow_key = str(workflow_key) if workflow_key else "default"
        allow_default_table = name == "run" and not workflow_arg

        if row and trigger.chat_id and (not ctx.bitable_mode.read_enabled) and ctx.bitable_configs and (ctx.settings.bitable_mode or "").strip().lower() not in ("off", "none", "disable", "disabled"):
            await _send_license_guidance(im, trigger)
            return
        
        if workflow_key == "default" and not ctx.workflows.get("default"):
            resolved_table_key = _pick_table_key_for_workflow(
                ctx,
                args=args,
                workflow_key=workflow_key,
                allow_default=allow_default_table,
            )
            picked = _pick_default_workflow_key(ctx, table_key=resolved_table_key)
            if picked:
                workflow_key = picked
        table_key = _pick_table_key_for_workflow(
            ctx,
            args=args,
            workflow_key=workflow_key,
            allow_default=allow_default_table,
        )

        if not record_id and not row:
            bitable = ctx.bitables.get(table_key) if table_key else None
            
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
            allow_default_table_fallback=allow_default_table,
        )
        await _send_text_by_trigger(im, trigger, f"已入队: {prompt_id or 'unknown'}")
        return

    if name in ("batch", "drain"):
        explicit_table_key = _pick_table_key(args)
        base_table_key = explicit_table_key or ctx.default_table_key
        workflow_key = _pick_workflow_key(args) or str(args.get("workflow") or "")
        
        if not workflow_key:
            workflow_key = _pick_default_workflow_key(ctx, table_key=base_table_key) or "default"
        
        if not workflow_key or (workflow_key == "default" and not ctx.workflows.get("default")):
            if trigger.chat_id:
                await _send_text_by_trigger(im, trigger, "缺少 workflow 且未找到默认工作流配置")
            return
            
        table_key = _pick_table_key_for_workflow(
            ctx,
            args=args,
            workflow_key=workflow_key,
            allow_default=True,
        )
        if not table_key:
            if trigger.chat_id:
                await _send_text_by_trigger(im, trigger, "缺少 table")
            return
            
        batch = int(str(args.get("batch") or args.get("limit") or "10"))
        inflight = int(str(args.get("inflight") or "1"))
        
        bitable = ctx.bitables.get(table_key)
        if bitable is None or not bitable.mode.read_enabled:
            if trigger.chat_id:
                if (not ctx.bitable_mode.read_enabled) and ctx.bitable_configs and (ctx.settings.bitable_mode or "").strip().lower() not in ("off", "none", "disable", "disabled"):
                    await _send_license_guidance(im, trigger)
                else:
                    await _send_text_by_trigger(im, trigger, "无法读取表格，无法执行批量(batch/drain)操作。")
            return
        if trigger.chat_id:
            await _send_text_by_trigger(im, trigger, f"已启动队列: {workflow_key} table={table_key}")
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
                await _send_text_by_trigger(im, trigger, f"队列启动失败: {e}")
        return

    if name == "stop_queue":
        workflow_key = _pick_workflow_key(args) or str(args.get("workflow") or "")
        explicit_table_key = _pick_table_key(args)
        base_table_key = explicit_table_key or ctx.default_table_key
        
        if not workflow_key:
            workflow_key = _pick_default_workflow_key(ctx, table_key=base_table_key) or "default"

        table_key = _pick_table_key_for_workflow(
            ctx,
            args=args,
            workflow_key=workflow_key,
            allow_default=True,
        )
        if workflow_key and table_key:
            await ctx.runner.stop(workflow_key=workflow_key, table_key=table_key)
            if trigger.chat_id:
                await _send_text_by_trigger(im, trigger, f"已停止队列: {workflow_key} table={table_key}")
        return

    await _send_text_by_trigger(im, trigger, f"未知指令: {name}")


def dispatch_in_thread(ctx: AppContext, *, name: str, args: dict[str, Any], trigger: TriggerContext) -> None:
    try:
        asyncio.run(dispatch(ctx, name=name, args=args, trigger=trigger))
    except Exception as e:
        logging.exception("dispatch failed: %s", e)
        try:
            im = IMClient(ctx.auth)
            if trigger.chat_id:
                asyncio.run(im.send_text(chat_id=trigger.chat_id, text=f"执行失败: {e}"))
            elif trigger.user_open_id:
                asyncio.run(im.send_text_to_open_id(open_id=trigger.user_open_id, text=f"执行失败: {e}"))
        except Exception:
            pass
