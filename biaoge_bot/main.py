from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from .network import configure_local_proxy_bypass
from .tls import configure_tls_ca_bundle

configure_local_proxy_bypass()
configure_tls_ca_bundle()

import httpx
import lark_oapi as lark
import uvicorn
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from .callback_server import create_callback_app
from .commands import parse_message_text
from .comfyui import ComfyUIClient
from .config import load_json_with_env, load_settings, normalize_config_paths, project_root
from .context import AppContext
from .dispatcher import TriggerContext, dispatch, dispatch_in_thread
from .feishu_auth import FeishuAuth
from .license_guard import check_license
from .logging_setup import DailyFileHandler
from .modules.bitable_logic import subscribe_bitable_files
from .modules.bitable_trigger import (
    extract_event_type as _enc_extract_bitable_event_type,
    extract_operator_open_id as _enc_extract_operator_open_id,
    extract_record_ids as _enc_extract_record_ids,
    extract_table_ref as _enc_extract_table_ref,
    resolve_table_key as _enc_resolve_table_key,
    try_trigger_record as _enc_try_trigger_record,
)
from .ports import BitableConfig, BitableMode, ctx_bitable_event_enabled, normalize_bitable_mode_name
from .queue_runner import QueueRunner
from .workflows import WorkflowRegistry

_BOT_OPEN_ID: str | None = None
_BOT_OPEN_ID_LOCK = threading.Lock()
_WS_EVENT_DUMP_LIMIT = 8000


class _DebugWsEventHandler:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def do_without_validation(self, payload: bytes) -> Any:
        s = ""
        try:
            s = payload.decode("utf-8", errors="replace")
        except Exception:
            try:
                s = str(payload)
            except Exception:
                s = ""
        event_type = ""
        schema = ""
        try:
            obj = json.loads(s) if s else {}
            if isinstance(obj, dict):
                header = obj.get("header") if isinstance(obj.get("header"), dict) else {}
                event = obj.get("event") if isinstance(obj.get("event"), dict) else {}
                schema = str(obj.get("schema") or "").strip()
                event_type = str(header.get("event_type") or header.get("eventType") or obj.get("event_type") or obj.get("eventType") or event.get("type") or obj.get("type") or "").strip()
        except Exception:
            obj = None
        try:
            if event_type:
                logging.info("ws_event (schema=%s event_type=%s)", schema or "", event_type)
            else:
                logging.info("ws_event (schema=%s event_type=unknown)", schema or "")
        except Exception:
            pass
        try:
            return self._inner.do_without_validation(payload)
        except Exception as e:
            try:
                msg = str(e or "")
                snippet = (s or "")[:_WS_EVENT_DUMP_LIMIT]
                logging.error("ws_event handler_error: %s. raw=%s", msg, snippet)
            except Exception:
                pass
            return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _all_bitable_file_tokens_from_config(ctx: AppContext) -> list[str]:
    out: list[str] = []
    for _, cfg in (getattr(ctx, "bitable_configs", None) or {}).items():
        try:
            token = str(getattr(cfg, "app_token", "") or "").strip()
        except Exception:
            token = ""
        if token and token not in out:
            out.append(token)
    return out


def _warm_bitable_event_subscriptions(ctx: AppContext) -> None:
    if not ctx_bitable_event_enabled(ctx):
        return

    async def _job() -> None:
        if not ctx_bitable_event_enabled(ctx):
            return
        ftokens = _all_bitable_file_tokens_from_config(ctx)
        if not ftokens:
            return
        try:
            results = await subscribe_bitable_files(auth=ctx.auth, file_tokens=ftokens)
        except Exception as e:
            try:
                logging.info("bitable_event_subscribe skipped: %s", str(e))
            except Exception:
                pass
            return
        for item in results:
            if bool(item.get("ok")):
                logging.info("bitable_event_subscribe ok (file_token=%s)", str(item.get("file_token") or ""))
            else:
                if item.get("error"):
                    logging.info("bitable_event_subscribe error (file_token=%s): %s", str(item.get("file_token") or ""), str(item.get("error") or ""))
                else:
                    logging.info(
                        "bitable_event_subscribe failed (file_token=%s status=%s body=%s)",
                        str(item.get("file_token") or ""),
                        str(item.get("status_code") or ""),
                        str(item.get("body") or "")[:500],
                    )

    try:
        asyncio.run(_job())
    except Exception:
        pass


async def _fetch_bot_open_id(ctx: AppContext) -> str | None:
    token = ""
    try:
        token = await ctx.auth.tenant_token()
    except Exception:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://open.feishu.cn/open-apis/bot/v3/info/",
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            data = r.json()
    except Exception:
        return None
    try:
        d0 = data.get("data") if isinstance(data, dict) else None
        if isinstance(d0, dict):
            b0 = d0.get("bot")
            if isinstance(b0, dict):
                for k in ("open_id", "openId", "bot_open_id", "botOpenId"):
                    v = b0.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
            for k in ("open_id", "openId", "bot_open_id", "botOpenId"):
                v = d0.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    except Exception:
        return None
    return None


def _get_bot_open_id(ctx: AppContext) -> str | None:
    v = str((os.environ.get("FEISHU_AT_USER_ID") or "").strip())
    if v:
        return v
    global _BOT_OPEN_ID
    with _BOT_OPEN_ID_LOCK:
        return _BOT_OPEN_ID


def _warm_bot_open_id(ctx: AppContext) -> None:
    global _BOT_OPEN_ID
    try:
        v = asyncio.run(_fetch_bot_open_id(ctx))
    except Exception:
        v = None
    if v:
        with _BOT_OPEN_ID_LOCK:
            _BOT_OPEN_ID = v


def _parse_message_content_json(msg_content: str) -> dict[str, Any]:
    try:
        obj = json.loads(msg_content or "{}")
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _collect_values_by_key(obj: Any, *, keys: set[str], limit: int = 10) -> list[Any]:
    out: list[Any] = []

    def walk(x: Any) -> None:
        if len(out) >= limit:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                if isinstance(k, str) and k in keys and len(out) < limit:
                    out.append(v)
                walk(v)
            return
        if isinstance(x, list):
            for it in x:
                walk(it)
            return

    walk(obj)
    return out


def _collect_mention_ids(msg: Any, content: dict[str, Any]) -> list[str]:
    out: list[str] = []
    ms = getattr(msg, "mentions", None)
    if isinstance(ms, list):
        for it in ms:
            try:
                if isinstance(it, str) and it.strip():
                    out.append(it.strip())
                    continue
                if isinstance(it, dict):
                    id0 = it.get("id")
                    if isinstance(id0, str) and id0.strip():
                        out.append(id0.strip())
                    elif isinstance(id0, dict):
                        for k in ("open_id", "openId", "user_id", "userId", "union_id", "unionId"):
                            v = id0.get(k)
                            if isinstance(v, str) and v.strip():
                                out.append(v.strip())
                    for k in ("open_id", "openId", "user_id", "userId", "union_id", "unionId"):
                        v = it.get(k)
                        if isinstance(v, str) and v.strip():
                            out.append(v.strip())
                    continue

                to_dict = getattr(it, "to_dict", None)
                if callable(to_dict):
                    d = to_dict()
                    if isinstance(d, dict):
                        id0 = d.get("id")
                        if isinstance(id0, str) and id0.strip():
                            out.append(id0.strip())
                        elif isinstance(id0, dict):
                            for k in ("open_id", "openId", "user_id", "userId", "union_id", "unionId"):
                                v = id0.get(k)
                                if isinstance(v, str) and v.strip():
                                    out.append(v.strip())

                mid = getattr(it, "id", None)
                if isinstance(mid, str) and mid.strip():
                    out.append(mid.strip())
                    continue
                v = getattr(mid, "open_id", None) if mid else None
                if isinstance(v, str) and v.strip():
                    out.append(v.strip())
                    continue
                v2 = getattr(mid, "user_id", None) if mid else None
                if isinstance(v2, str) and v2.strip():
                    out.append(v2.strip())
                    continue
                v3 = getattr(mid, "union_id", None) if mid else None
                if isinstance(v3, str) and v3.strip():
                    out.append(v3.strip())
            except Exception:
                continue

    raw = content.get("mentions")
    if isinstance(raw, list):
        for it in raw:
            if not isinstance(it, dict):
                continue
            id0 = it.get("id")
            if isinstance(id0, dict):
                for k in ("open_id", "openId", "user_id", "userId", "union_id", "unionId"):
                    v = id0.get(k)
                    if isinstance(v, str) and v.strip():
                        out.append(v.strip())
            elif isinstance(id0, str) and id0.strip():
                out.append(id0.strip())
            for k in ("open_id", "openId", "user_id", "userId", "union_id", "unionId"):
                v = it.get(k)
                if isinstance(v, str) and v.strip():
                    out.append(v.strip())
    return out



def _is_group_chat(msg: Any) -> bool:
    t = getattr(msg, "chat_type", None)
    if isinstance(t, str) and t.strip().lower() == "p2p":
        return False
    return True


def _should_accept_command_in_message(ctx: AppContext, *, msg: Any, content: dict[str, Any], text: str) -> bool:
    if not _is_group_chat(msg):
        return True
    raw = str(text or "").strip()
    norm = raw.replace("\u00a0", " ").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "")
    norm = norm.replace("：", ":").replace("／", "/").replace("∕", "/")
    for tok in re.split(r"\s+", norm.strip()):
        if tok.startswith("::/") and len(tok) > 3:
            return True
    bot_open_id = _get_bot_open_id(ctx)
    ids = _collect_mention_ids(msg, content)
    if bot_open_id and bot_open_id in ids:
        return True
    if not bot_open_id:
        if ids:
            return True
        if "<at" in (text or ""):
            return True
    return False


def _default_bitable_fields() -> dict[str, str]:
    return {
        "status": "任务状态",
        "workflow": "工作流",
        "output": "生成结果",
        "error": "错误信息",
        "prompt_id": "prompt_id",
        "created_time": "创建时间",
    }


def _default_bitable_status_values() -> dict[str, str]:
    return {"queued": "待处理", "trigger": "触发执行", "running": "执行中", "done": "已完成", "failed": "生成失败"}


def _parse_tables(settings: Any, cfg: dict[str, Any]) -> tuple[dict[str, BitableConfig], str | None]:
    tables: dict[str, BitableConfig] = {}
    default_key: str | None = None

    raw_tables = cfg.get("tables")
    if isinstance(raw_tables, dict) and raw_tables:
        for key, raw in raw_tables.items():
            if not isinstance(raw, dict):
                continue
            app_token = str(raw.get("app_token") or "").strip()
            table_id = str(raw.get("table_id") or "").strip()
            view_id = str(raw.get("view_id") or "").strip() or None
            if not app_token or not table_id:
                continue
            fields = raw.get("fields") or _default_bitable_fields()
            status_values = raw.get("status_values") or _default_bitable_status_values()
            tables[str(key)] = BitableConfig(
                app_token=app_token,
                table_id=table_id,
                view_id=view_id,
                fields=dict(fields),
                status_values=dict(status_values),
            )
        dk = cfg.get("default_table")
        if isinstance(dk, str) and dk in tables:
            default_key = dk
        elif tables:
            default_key = next(iter(tables.keys()))
        return tables, default_key

    raw = cfg.get("bitable") or {}
    if isinstance(raw, dict):
        app_token = str(raw.get("app_token") or settings.bitable_app_token or "").strip()
        table_id = str(raw.get("table_id") or settings.bitable_table_id or "").strip()
        view_id = str(raw.get("view_id") or "").strip() or None
        if app_token and table_id:
            fields = raw.get("fields") or _default_bitable_fields()
            status_values = raw.get("status_values") or _default_bitable_status_values()
            tables["default"] = BitableConfig(
                app_token=app_token,
                table_id=table_id,
                view_id=view_id,
                fields=dict(fields),
                status_values=dict(status_values),
            )
            default_key = "default"

    return tables, default_key


def _parse_bitable_mode(mode: str) -> BitableMode:
    m = normalize_bitable_mode_name(mode)
    if m == "off":
        return BitableMode(read_enabled=False, write_enabled=False)
    if m == "read":
        return BitableMode(read_enabled=True, write_enabled=False)
    if m == "write":
        # write mode still needs minimal reads for locating write-back records
        # (for example row=...), but business logic must not use record fields
        # as workflow input parameters.
        return BitableMode(read_enabled=True, write_enabled=True)
    if m == "readwrite":
        return BitableMode(read_enabled=True, write_enabled=True)
    return BitableMode(read_enabled=True, write_enabled=True)


def build_context(env_file: str | None = None) -> AppContext:
    settings = load_settings(env_file)
    configure_local_proxy_bypass([settings.comfyui_base_url, settings.callback_host])
    configure_tls_ca_bundle(root=settings.project_root)
    if settings.workflow_config_path:
        p = Path(settings.workflow_config_path)
        if not p.exists():
            raise RuntimeError(f"WORKFLOW_CONFIG_PATH not found: {settings.workflow_config_path}")
        cfg = load_json_with_env(p)
    else:
        root = project_root()
        p = root / "config" / "workflows.local.json"
        if p.exists():
            cfg = load_json_with_env(p)
        else:
            cfg = {}
    cfg = normalize_config_paths(cfg, root=settings.project_root)
    auth = FeishuAuth(settings.feishu_app_id, settings.feishu_app_secret)
    if settings.bitable_mode == "auto":
        tables, default_table_key = _parse_tables(settings, cfg)
        bitable_mode = BitableMode(read_enabled=bool(tables), write_enabled=bool(tables))
    else:
        tables, default_table_key = _parse_tables(settings, cfg)
        bitable_mode = _parse_bitable_mode(settings.bitable_mode)
    if tables and (bitable_mode.read_enabled or bitable_mode.write_enabled):
        lic = check_license()
        if not lic.ok:
            logging.warning("未检测到有效授权，BITABLE_MODE=%s 将不会启用 Bitable/Drive。", settings.bitable_mode)
            logging.warning("设备码: %s", lic.device_code)
            logging.warning("请将 license.lic 放置到: %s", str(lic.license_path))
            logging.warning("将设备码发给授权方生成 license.lic，放置后重启。")
            bitable_mode = BitableMode(read_enabled=False, write_enabled=False)
    drive = None
    bitables: dict[str, Any] = {}
    if tables and (bitable_mode.read_enabled or bitable_mode.write_enabled):
        try:
            from .modules.bitable import BitableClient
            from .modules.drive import DriveClient
        except Exception as e:
            raise RuntimeError("bitable enabled but module not available") from e
        for key, table_cfg in tables.items():
            bitables[key] = BitableClient(auth, table_cfg, bitable_mode)
        drive = DriveClient(auth)
    comfyui = ComfyUIClient(settings.comfyui_base_url, upload_timeout_seconds=settings.comfyui_upload_timeout_seconds)
    workflows = WorkflowRegistry.from_config(cfg)
    runner = QueueRunner()
    default_workflow_key = None
    dw = cfg.get("default_workflow") or cfg.get("defaultWorkflow")
    if isinstance(dw, str) and dw.strip() and workflows.get(dw.strip()):
        default_workflow_key = dw.strip()
    return AppContext(
        settings=settings,
        config=cfg,
        auth=auth,
        bitables=bitables,
        drive=drive,
        comfyui=comfyui,
        workflows=workflows,
        bitable_mode=bitable_mode,
        bitable_configs=tables,
        default_table_key=default_table_key,
        default_workflow_key=default_workflow_key,
        runner=runner,
    )


def start_callback_server(ctx: AppContext) -> None:
    app = create_callback_app(ctx)
    config = uvicorn.Config(app, host=ctx.settings.callback_host, port=ctx.settings.callback_port, log_level="info", access_log=True)
    server = uvicorn.Server(config)
    server.run()


def _extract_text_from_message_content(msg_content: str) -> str:
    try:
        content = json.loads(msg_content or "{}")
        if isinstance(content, dict):
            v = content.get("text")
            if isinstance(v, str) and v.strip():
                return v.strip()

            out: list[str] = []

            def walk(x: Any) -> None:
                if len(out) >= 24:
                    return
                if isinstance(x, dict):
                    tv = x.get("text")
                    if isinstance(tv, str):
                        s = tv.strip()
                        if s:
                            out.append(s)
                            if len(out) >= 24:
                                return
                    for vv in x.values():
                        walk(vv)
                    return
                if isinstance(x, list):
                    for it in x:
                        walk(it)
                    return

            walk(content)
            if out:
                s2 = " ".join(out).strip()
                return s2[:2000]
    except Exception:
        pass
    return ""


def do_p2_im_message_receive_v1_factory(ctx: AppContext):
    def handler(data: P2ImMessageReceiveV1) -> None:
        msg = data.event.message
        content = _parse_message_content_json(msg.content)
        chat_id = getattr(msg, "chat_id", None)
        msg_id = getattr(msg, "message_id", None)
        msg_type = getattr(msg, "message_type", None)
        chat_type = getattr(msg, "chat_type", None)
        sender = getattr(data.event, "sender", None)
        sender_id = getattr(sender, "sender_id", None) if sender else None
        user_open_id = getattr(sender_id, "open_id", None) if sender_id else None
        if not user_open_id:
            user_open_id = getattr(sender_id, "user_id", None) if sender_id else None

        try:
            img_vals = _collect_values_by_key(content, keys={"image_key", "imageKey"}, limit=5)
            for v in img_vals:
                if isinstance(v, str) and v.strip():
                    ctx.runner.register_im_attachment(chat_id=chat_id, user_open_id=user_open_id, kind="image", key=v.strip(), message_id=msg_id)
            file_vals = _collect_values_by_key(content, keys={"file_key", "fileKey"}, limit=5)
            for v in file_vals:
                if isinstance(v, str) and v.strip():
                    ctx.runner.register_im_attachment(chat_id=chat_id, user_open_id=user_open_id, kind="file", key=v.strip(), message_id=msg_id)
        except Exception:
            pass

        text = str(content.get("text") or "")
        if not text:
            text = _extract_text_from_message_content(msg.content)
        if not text:
            try:
                text = _extract_text_from_message_content(json.dumps(content, ensure_ascii=False))
            except Exception:
                text = ""
        if ("::" in (text or "")) or ("：：" in (text or "")) or ("::" in (msg.content or "")) or ("：：" in (msg.content or "")):
            try:
                raw_preview = str(msg.content or "")[:600]
                t_preview = str(text or "")[:200]
                head_codes = [str(ord(c)) for c in t_preview[:10]]
                logging.info(
                    "trigger_probe (chat_id=%s chat_type=%s msg_type=%s user=%s text=%s text_codes=%s raw=%s)",
                    str(chat_id or ""),
                    str(chat_type or ""),
                    str(msg_type or ""),
                    str(user_open_id or ""),
                    t_preview,
                    ",".join(head_codes),
                    raw_preview,
                )
            except Exception:
                pass
        cmd = parse_message_text(text)
        if not cmd:
            return
        if text and not _should_accept_command_in_message(ctx, msg=msg, content=content, text=text):
            try:
                bot_open_id = _get_bot_open_id(ctx)
                ids = _collect_mention_ids(msg, content)
                logging.info(
                    "ignored command in group chat (chat_id=%s user=%s cmd=%s). bot_open_id=%s mention_ids=%s text=%s",
                    str(chat_id or ""),
                    str(user_open_id or ""),
                    str(getattr(cmd, "name", "") or ""),
                    str(bot_open_id or ""),
                    ids,
                    (text or "")[:120],
                )
            except Exception:
                pass
            return
        try:
            logging.info(
                "accepted command (chat_id=%s user=%s cmd=%s args=%s)",
                str(chat_id or ""),
                str(user_open_id or ""),
                str(getattr(cmd, "name", "") or ""),
                getattr(cmd, "args", None),
            )
        except Exception:
            pass
        trig = TriggerContext(chat_id=chat_id, user_open_id=user_open_id, source="im.message.receive_v1")
        threading.Thread(target=dispatch_in_thread, kwargs={"ctx": ctx, "name": cmd.name, "args": cmd.args, "trigger": trig}, daemon=True).start()

    return handler


def do_card_action_trigger_factory(ctx: AppContext):
    def handler(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        value = (data.event.action.value or {}) if data and data.event and data.event.action else {}
        if not isinstance(value, dict):
            value = {}
        name = str(value.get("cmd") or "run_default")
        args = value.get("args") or {}
        if not isinstance(args, dict):
            args = {}

        user_open_id = None
        operator = getattr(data.event, "operator", None)
        oper_id = getattr(operator, "open_id", None) if operator else None
        if isinstance(oper_id, str):
            user_open_id = oper_id

        trig = TriggerContext(chat_id=None, user_open_id=user_open_id, source="card.action.trigger")
        threading.Thread(target=dispatch_in_thread, kwargs={"ctx": ctx, "name": name, "args": args, "trigger": trig}, daemon=True).start()
        
        # Determine appropriate toast type
        # Lark OAPI for P2CardActionTriggerResponse toast supports types: "success", "error", "info", "warning"
        # The correct value for success seems to be "success" or "info". 
        # But if "success" shows up as red/error in the UI, we should stick to "info" for normal flow
        # or use "success" if it's the correct enum but the UI rendered it wrong.
        # Actually,飞书卡片的回调Toast官方枚举是 "info", "success", "error", "warning"
        # 飞书卡片 Toast 的 type 严格限制，但在很多版本的飞书客户端中
        # "success" (绿底) 会被渲染为红色错误样式，反而是 "info" 能正常显示。
        # 这是飞书客户端的历史遗留 bug。为了给用户正确的心智体验，这里使用 info 兜底。
        toast_type = "info"

        # We explicitly avoid using "success" to prevent the red error styling bug in Lark.
        if name in ("reset", "reset_table", "stop_queue"):
            toast_type = "warning"
            
        return P2CardActionTriggerResponse({"toast": {"type": toast_type, "content": "已收到，开始执行"}})

    return handler


def do_bot_menu_event_factory(ctx: AppContext):
    def _normalize_payload(data: Any) -> dict[str, Any]:
        if isinstance(data, dict):
            return data
        to_dict = getattr(data, "to_dict", None)
        if callable(to_dict):
            try:
                d = to_dict()
                if isinstance(d, dict):
                    return d
            except Exception:
                pass
        to_dict2 = getattr(data, "dict", None)
        if callable(to_dict2):
            try:
                d2 = to_dict2()
                if isinstance(d2, dict):
                    return d2
            except Exception:
                pass
        if isinstance(data, str) and data.strip():
            try:
                d3 = json.loads(data)
                if isinstance(d3, dict):
                    return d3
            except Exception:
                pass
        d4 = getattr(data, "__dict__", None)
        return d4 if isinstance(d4, dict) else {}

    def _extract_event_type(payload: dict[str, Any]) -> str:
        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        v = header.get("event_type") or header.get("eventType") or payload.get("event_type") or payload.get("eventType")
        return str(v).strip() if isinstance(v, str) and v.strip() else ""

    def _extract_event_key(payload: dict[str, Any]) -> str | None:
        vals = _collect_values_by_key(payload, keys={"event_key", "eventKey", "key", "menu_key", "menuKey"}, limit=10)
        for v in vals:
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    def _extract_chat_id(payload: dict[str, Any]) -> str | None:
        vals = _collect_values_by_key(payload, keys={"chat_id", "chatId"}, limit=10)
        for v in vals:
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    def _extract_user_open_id(payload: dict[str, Any]) -> str | None:
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        operator = event.get("operator") if isinstance(event.get("operator"), dict) else {}
        oper_id = operator.get("open_id") or operator.get("openId") or operator.get("user_id") or operator.get("userId")
        if isinstance(oper_id, str) and oper_id.strip():
            return oper_id.strip()
        vals = _collect_values_by_key(payload, keys={"open_id", "openId"}, limit=10)
        for v in vals:
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    def _event_key_to_command_text(event_key: str) -> str:
        ev = str(event_key or "").strip()
        if not ev:
            return ""
        if ev.startswith("cmd_"):
            ev = ev[4:].strip()
        if ev.startswith("/"):
            return ev
        if "__" in ev:
            parts = [p for p in ev.split("__") if p]
            if parts:
                return "/" + parts[0] + ((" " + " ".join(parts[1:])) if len(parts) > 1 else "")
        if ":" in ev:
            parts2 = [p for p in ev.split(":") if p]
            if parts2:
                return "/" + parts2[0] + ((" " + " ".join(parts2[1:])) if len(parts2) > 1 else "")
        return f"/{ev}"

    def handler(data: Any) -> None:
        payload = _normalize_payload(data)
        event_type = _extract_event_type(payload)
        event_key = _extract_event_key(payload)
        chat_id0 = _extract_chat_id(payload)
        user_open_id0 = _extract_user_open_id(payload)

        try:
            logging.info(
                "menu_event received (event_type=%s event_key=%s chat_id=%s user_open_id=%s keys=%s)",
                event_type or "unknown",
                str(event_key or ""),
                str(chat_id0 or ""),
                str(user_open_id0 or ""),
                ",".join(sorted([str(k) for k in payload.keys()])) if isinstance(payload, dict) else "",
            )
        except Exception:
            pass

        if not event_key:
            try:
                logging.info("menu_event ignored: missing event_key (event_type=%s)", event_type or "unknown")
            except Exception:
                pass
            return
        text = _event_key_to_command_text(event_key)
        if not text:
            try:
                logging.info("menu_event ignored: empty cmd_text (event_key=%s)", str(event_key or ""))
            except Exception:
                pass
            return
        cmd = parse_message_text(text)
        if not cmd:
            try:
                logging.info("menu_event ignored: invalid command (cmd_text=%s)", str(text or ""))
            except Exception:
                pass
            return
        chat_id = chat_id0
        user_open_id = user_open_id0
        try:
            logging.info(
                "menu_event -> command (cmd_text=%s name=%s args=%s chat_id=%s user_open_id=%s)",
                str(text or ""),
                str(cmd.name or ""),
                json.dumps(cmd.args or {}, ensure_ascii=False),
                str(chat_id or ""),
                str(user_open_id or ""),
            )
        except Exception:
            pass
        trig = TriggerContext(chat_id=chat_id, user_open_id=user_open_id, source="bot.menu")
        threading.Thread(target=dispatch_in_thread, kwargs={"ctx": ctx, "name": cmd.name, "args": cmd.args, "trigger": trig}, daemon=True).start()

    return handler


_BITABLE_TRIGGER_LOCK = threading.Lock()
_BITABLE_TRIGGER_SEEN: dict[str, float] = {}


def do_bitable_record_changed_event_factory(ctx: AppContext):
    def _normalize_payload(data: Any) -> dict[str, Any]:
        if isinstance(data, dict):
            return data
        to_dict = getattr(data, "to_dict", None)
        if callable(to_dict):
            try:
                d = to_dict()
                if isinstance(d, dict):
                    return d
            except Exception:
                pass
        to_dict2 = getattr(data, "dict", None)
        if callable(to_dict2):
            try:
                d2 = to_dict2()
                if isinstance(d2, dict):
                    return d2
            except Exception:
                pass
        if isinstance(data, str) and data.strip():
            try:
                d3 = json.loads(data)
                if isinstance(d3, dict):
                    return d3
            except Exception:
                pass
        d4 = getattr(data, "__dict__", None)
        return d4 if isinstance(d4, dict) else {}

    def handler(data: Any) -> None:
        if not ctx_bitable_event_enabled(ctx):
            return
        payload = _normalize_payload(data)
        event_type = _enc_extract_bitable_event_type(payload) or "unknown"
        operator_open_id = _enc_extract_operator_open_id(payload, collect_values_by_key=_collect_values_by_key)
        app_token, table_id = _enc_extract_table_ref(payload, collect_values_by_key=_collect_values_by_key)
        record_ids = _enc_extract_record_ids(payload, collect_values_by_key=_collect_values_by_key)
        record_id = record_ids[0] if record_ids else None
        table_key = _enc_resolve_table_key(ctx, app_token=app_token, table_id=table_id)
        try:
            logging.info(
                "bitable_event received (event_type=%s table_key=%s record_id=%s operator_open_id=%s app_token=%s table_id=%s keys=%s)",
                event_type,
                str(table_key or ""),
                str(record_id or ""),
                str(operator_open_id or ""),
                str(app_token or ""),
                str(table_id or ""),
                ",".join(sorted([str(k) for k in payload.keys()])) if isinstance(payload, dict) else "",
            )
        except Exception:
            pass

        if not table_key or not record_id:
            return
        if not operator_open_id:
            operator_open_id = None

        trig_key = f"{table_key}:{record_id}:{event_type}"
        now = float(time.time())
        with _BITABLE_TRIGGER_LOCK:
            last = _BITABLE_TRIGGER_SEEN.get(trig_key)
            if last and now - last < 2.0:
                return
            _BITABLE_TRIGGER_SEEN[trig_key] = now
            if len(_BITABLE_TRIGGER_SEEN) > 2000:
                for k, t in list(_BITABLE_TRIGGER_SEEN.items()):
                    if now - t > 120.0:
                        _BITABLE_TRIGGER_SEEN.pop(k, None)

        def _run() -> None:
            async def _job() -> None:
                for rid in (record_ids or [])[:20]:
                    try:
                        await _enc_try_trigger_record(ctx, table_key=table_key, record_id=rid, operator_open_id=operator_open_id, payload=payload)
                    except Exception:
                        continue

            asyncio.run(_job())

        threading.Thread(target=_run, daemon=True).start()

    return handler


def main() -> None:
    try:
        ctx = build_context()
    except RuntimeError as e:
        msg = str(e or "").strip()
        if msg.startswith("missing env:"):
            key = msg.split(":", 1)[1].strip() if ":" in msg else ""
            print("")
            print("启动失败：缺少必要配置。")
            if key:
                print(f"缺少环境变量：{key}")
            print("")
            print("解决方法：")
            print("1) 打开项目根目录下的 .env 文件")
            print("2) 填上飞书应用的 FEISHU_APP_ID 和 FEISHU_APP_SECRET（以及你的表格配置相关参数）")
            print("3) 保存后重新运行 start.cmd")
        else:
            print("")
            print(f"启动失败：{msg}")
            print("")
            print("请检查 .env 中的配置是否正确，或运行 start.cmd 重新交互式配置。")
        raise SystemExit(1)
    log_level = getattr(logging, ctx.settings.bot_log_level.upper(), logging.INFO)
    log_dir = Path(ctx.settings.project_root) / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers: list[logging.Handler] = [logging.StreamHandler()]
        if os.environ.get("BIAOGE_SUPERVISOR_CAPTURE", "").strip() != "1":
            handlers.append(DailyFileHandler(ctx.settings.project_root))
    except Exception:
        handlers = [logging.StreamHandler()]
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s",
        handlers=handlers,
    )
    try:
        ctx.runner.set_context(ctx)
    except Exception:
        pass
    try:
        if hasattr(ctx.runner, "start_remote_poller"):
            ctx.runner.start_remote_poller()
    except Exception:
        pass

    threading.Thread(target=start_callback_server, args=(ctx,), daemon=True).start()
    threading.Thread(target=_warm_bot_open_id, args=(ctx,), daemon=True).start()
    if ctx_bitable_event_enabled(ctx):
        threading.Thread(target=_warm_bitable_event_subscriptions, args=(ctx,), daemon=True).start()

    def _ignore_event(*_: Any, **__: Any) -> None:
        return

    builder = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1_factory(ctx))
        .register_p2_card_action_trigger(do_card_action_trigger_factory(ctx))
    )
    if hasattr(builder, "register_p2_customized_event"):
        getattr(builder, "register_p2_customized_event")("im.message.reaction.created_v1", _ignore_event)
        getattr(builder, "register_p2_customized_event")("im.message.reaction.deleted_v1", _ignore_event)
        try:
            getattr(builder, "register_p2_customized_event")("drive.file.edit_v1", _ignore_event)
        except Exception:
            pass
        menu_handler = do_bot_menu_event_factory(ctx)
        for et in (
            "application.bot.menu_v6",
            "application.bot.menu",
            "im.chat.menu_v6",
            "im.chat.menu_click",
            "im.chat.menu.click",
            "im.bot.menu_v6",
        ):
            try:
                getattr(builder, "register_p2_customized_event")(et, menu_handler)
            except Exception:
                pass
        if ctx_bitable_event_enabled(ctx):
            bitable_handler = do_bitable_record_changed_event_factory(ctx)
            for et in (
                "file_bitable_record_changed_v1",
                "file.bitable_record_changed_v1",
                "drive.file.bitable_record_changed_v1",
                "drive.file.bitable_record_changed",
                "bitable.record.changed_v1",
                "bitable.record.updated_v1",
                "bitable.record.created_v1",
                "bitable.record.deleted_v1",
                "bitable_record_changed_v1",
                "bitable.record_changed_v1",
            ):
                try:
                    getattr(builder, "register_p2_customized_event")(et, bitable_handler)
                except Exception:
                    pass
                try:
                    builder.register_p1_customized_event(et, bitable_handler)
                except Exception:
                    pass
    if hasattr(builder, "register_p2_im_message_message_read_v1"):
        getattr(builder, "register_p2_im_message_message_read_v1")(_ignore_event)
    else:
        if hasattr(builder, "register_p2_customized_event"):
            getattr(builder, "register_p2_customized_event")("im.message.message_read_v1", _ignore_event)
        builder.register_p1_customized_event("im.message.message_read_v1", _ignore_event)
    if hasattr(builder, "register_p2_customized_event"):
        getattr(builder, "register_p2_customized_event")("im.chat.access_event.bot_p2p_chat_entered_v1", _ignore_event)
    builder.register_p1_customized_event("im.chat.access_event.bot_p2p_chat_entered_v1", _ignore_event)

    event_handler = _DebugWsEventHandler(builder.build())

    delay = 5
    while True:
        try:
            logging.info("feishu socket client starting")
            cli = lark.ws.Client(ctx.settings.feishu_app_id, ctx.settings.feishu_app_secret, event_handler=event_handler, log_level=lark.LogLevel.INFO)
            cli.start()
            logging.warning("feishu socket client returned; restarting in %ss", delay)
        except KeyboardInterrupt:
            raise
        except Exception:
            logging.exception("feishu socket client crashed; restarting in %ss", delay)
        time.sleep(delay)
        delay = min(delay * 2, 60)


if __name__ == "__main__":
    main()
