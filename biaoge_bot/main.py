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
from .config import load_json_with_env, load_settings
from .context import AppContext
from .dispatcher import TriggerContext, dispatch, dispatch_in_thread
from .feishu_auth import FeishuAuth
from .license_guard import check_license
from .ports import BitableConfig, BitableMode
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
    async def _job() -> None:
        try:
            token = await ctx.auth.tenant_token()
        except Exception as e:
            try:
                logging.info("bitable_event_subscribe skipped: tenant_token error: %s", str(e))
            except Exception:
                pass
            return
        ftokens = _all_bitable_file_tokens_from_config(ctx)
        if not ftokens:
            return
        async with httpx.AsyncClient(timeout=10) as client:
            for file_token in ftokens[:20]:
                url = f"https://open.feishu.cn/open-apis/drive/v1/files/{file_token}/subscribe"
                try:
                    r = await client.post(url, headers={"Authorization": f"Bearer {token}"}, params={"file_type": "bitable"})
                    ok = r.status_code < 400
                    data = r.json() if ok else None
                    if ok and isinstance(data, dict) and data.get("code") in (0, None):
                        logging.info("bitable_event_subscribe ok (file_token=%s)", file_token)
                    else:
                        logging.info("bitable_event_subscribe failed (file_token=%s status=%s body=%s)", file_token, str(r.status_code), (r.text or "")[:500])
                except Exception as e:
                    try:
                        logging.info("bitable_event_subscribe error (file_token=%s): %s", file_token, str(e))
                    except Exception:
                        pass

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
        "trigger_cmd": "触发指令",
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
    m = (mode or "").strip().lower()
    if m in ("off", "none", "disable", "disabled"):
        return BitableMode(read_enabled=False, write_enabled=False)
    if m in ("read", "readonly", "ro"):
        return BitableMode(read_enabled=True, write_enabled=False)
    if m in ("write", "writeonly", "wo"):
        return BitableMode(read_enabled=True, write_enabled=True)
    if m in ("readwrite", "rw", "all", "on", "enable", "enabled"):
        return BitableMode(read_enabled=True, write_enabled=True)
    return BitableMode(read_enabled=True, write_enabled=True)


def build_context(env_file: str | None = None) -> AppContext:
    settings = load_settings(env_file)
    if settings.workflow_config_path:
        p = Path(settings.workflow_config_path)
        if not p.exists():
            raise RuntimeError(f"WORKFLOW_CONFIG_PATH not found: {settings.workflow_config_path}")
        cfg = load_json_with_env(p)
    else:
        root = Path(__file__).resolve().parent.parent
        p = root / "config" / "workflows.local.json"
        if p.exists():
            cfg = load_json_with_env(p)
        else:
            cfg = {}
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
    comfyui = ComfyUIClient(settings.comfyui_base_url)
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

    def _extract_event_type(payload: dict[str, Any]) -> str:
        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        v = header.get("event_type") or header.get("eventType") or payload.get("event_type") or payload.get("eventType") or payload.get("type")
        return str(v).strip() if isinstance(v, str) and v.strip() else ""

    def _extract_operator_open_id(payload: dict[str, Any]) -> str | None:
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        operator = event.get("operator") if isinstance(event.get("operator"), dict) else {}
        oper_id = operator.get("open_id") or operator.get("openId") or operator.get("user_id") or operator.get("userId")
        if isinstance(oper_id, str) and oper_id.strip():
            return oper_id.strip()

        operator_id = event.get("operator_id") if isinstance(event.get("operator_id"), dict) else {}
        oper2 = operator_id.get("open_id") or operator_id.get("openId") or operator_id.get("user_id") or operator_id.get("userId")
        if isinstance(oper2, str) and oper2.strip():
            return oper2.strip()

        op_list = event.get("operator_id_list")
        if isinstance(op_list, list) and op_list:
            first = op_list[0] if isinstance(op_list[0], dict) else {}
            oper3 = first.get("open_id") or first.get("openId") or first.get("user_id") or first.get("userId")
            if isinstance(oper3, str) and oper3.strip():
                return oper3.strip()

        vals = _collect_values_by_key(payload, keys={"operator_open_id", "operatorOpenId", "open_id", "openId"}, limit=10)
        for v in vals:
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    def _extract_table_ref(payload: dict[str, Any]) -> tuple[str | None, str | None]:
        app_token = None
        table_id = None
        vals = _collect_values_by_key(payload, keys={"app_token", "appToken", "file_token", "fileToken"}, limit=10)
        for v in vals:
            if isinstance(v, str) and v.strip():
                app_token = v.strip()
                break
        vals2 = _collect_values_by_key(payload, keys={"table_id", "tableId"}, limit=10)
        for v in vals2:
            if isinstance(v, str) and v.strip():
                table_id = v.strip()
                break
        return app_token, table_id

    def _extract_record_id(payload: dict[str, Any]) -> str | None:
        vals = _collect_values_by_key(payload, keys={"record_id", "recordId"}, limit=10)
        for v in vals:
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    def _extract_record_ids(payload: dict[str, Any]) -> list[str]:
        vals = _collect_values_by_key(payload, keys={"record_id", "recordId"}, limit=50)
        out: list[str] = []
        for v in vals:
            if isinstance(v, str) and v.strip():
                s = v.strip()
                if s not in out:
                    out.append(s)
        return out

    def _resolve_table_key(app_token: str | None, table_id: str | None) -> str | None:
        at = str(app_token or "").strip()
        tid = str(table_id or "").strip()
        if not at or not tid:
            return None
        for k, c in (ctx.bitable_configs or {}).items():
            try:
                if str(getattr(c, "app_token", "") or "") == at and str(getattr(c, "table_id", "") or "") == tid:
                    return str(k)
            except Exception:
                continue
        return None

    def _field_texts(value: Any) -> list[str]:
        out: list[str] = []
        if value is None:
            return out
        if isinstance(value, str):
            s = value.strip()
            return [s] if s else out
        if isinstance(value, (int, float, bool)):
            return [str(value)]
        if isinstance(value, dict):
            # 先拿给人看的文字，避免把下拉选项的内部 id（像 optxxxx）当成状态名。
            for k in ("text", "name", "title", "label", "display_value", "displayValue"):
                v = value.get(k)
                if isinstance(v, str) and v.strip():
                    out.append(v.strip())
                elif isinstance(v, (list, dict)):
                    out.extend(_field_texts(v))
            if out:
                return [x for x in out if x]
            # 上面确实没有“可读文本”时，再退回 value，兼容某些简单字段结构。
            v0 = value.get("value")
            if isinstance(v0, str) and v0.strip():
                out.append(v0.strip())
            elif isinstance(v0, (list, dict)):
                out.extend(_field_texts(v0))
            return [x for x in out if x]
        if isinstance(value, list):
            for it in value:
                out.extend(_field_texts(it))
            return [x for x in out if x]
        return out

    def _extract_open_id_from_field(value: Any) -> str | None:
        if isinstance(value, dict):
            for k in ("open_id", "openId"):
                v = value.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            id0 = value.get("id")
            if isinstance(id0, dict):
                for k in ("open_id", "openId"):
                    v = id0.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
        if isinstance(value, list):
            for it in value:
                v2 = _extract_open_id_from_field(it)
                if v2:
                    return v2
        return None

    def _pick_trigger_field_name(table_cfg: Any) -> str | None:
        fields_cfg = dict(getattr(table_cfg, "fields", {}) or {})
        for k in ("trigger_cmd", "trigger", "cmd", "command"):
            v = fields_cfg.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        auto = ctx.config.get("automation") if isinstance(ctx.config, dict) else None
        if isinstance(auto, dict):
            for k in ("trigger_cmd_field", "triggerCmdField", "trigger_field", "triggerField", "cmd_field", "cmdField"):
                v = auto.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return None

    def _pick_trigger_user_field_name(table_cfg: Any) -> str | None:
        fields_cfg = dict(getattr(table_cfg, "fields", {}) or {})
        for k in ("trigger_user", "triggerUser", "operator", "operator_open_id"):
            v = fields_cfg.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        auto = ctx.config.get("automation") if isinstance(ctx.config, dict) else None
        if isinstance(auto, dict):
            for k in ("trigger_user_field", "triggerUserField", "operator_field", "operatorField"):
                v = auto.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return None

    async def _find_field_meta_by_name(bitable: Any, field_name: str | None) -> dict[str, Any] | None:
        fn = str(field_name or "").strip()
        if not fn or not hasattr(bitable, "list_fields"):
            return None
        try:
            items = await bitable.list_fields()
        except Exception:
            return None
        if not isinstance(items, list):
            return None
        for it in items:
            if not isinstance(it, dict):
                continue
            name = it.get("field_name")
            if str(name or "").strip() == fn:
                return it
        return None

    def _field_meta_id(field_meta: dict[str, Any] | None) -> str | None:
        if not isinstance(field_meta, dict):
            return None
        fid = field_meta.get("field_id")
        if isinstance(fid, str) and fid.strip():
            return fid.strip()
        return None

    def _field_meta_property(field_meta: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(field_meta, dict):
            return {}
        raw = field_meta.get("property")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            s = raw.strip()
            if not s:
                return {}
            try:
                obj = json.loads(s)
            except Exception:
                return {}
            return obj if isinstance(obj, dict) else {}
        return {}

    def _field_option_maps(field_meta: dict[str, Any] | None) -> tuple[dict[str, str], dict[str, str]]:
        id_to_name: dict[str, str] = {}
        name_to_id: dict[str, str] = {}
        prop = _field_meta_property(field_meta)
        options = prop.get("options")
        if not isinstance(options, list):
            return id_to_name, name_to_id
        for item in options:
            if not isinstance(item, dict):
                continue
            oid = item.get("id")
            name = item.get("name")
            oid_s = str(oid).strip() if isinstance(oid, str) else ""
            name_s = str(name).strip() if isinstance(name, str) else ""
            if oid_s and name_s:
                id_to_name[oid_s] = name_s
                name_to_id[name_s] = oid_s
        return id_to_name, name_to_id

    def _normalize_field_texts_for_meta(raw: Any, field_meta: dict[str, Any] | None) -> list[str]:
        base = _event_field_value_texts(raw)
        if not base:
            return []
        id_to_name, _ = _field_option_maps(field_meta)
        out: list[str] = []
        for item in base:
            s = str(item or "").strip()
            if not s:
                continue
            out.append(id_to_name.get(s, s))
        dedup: list[str] = []
        for item in out:
            if item not in dedup:
                dedup.append(item)
        return dedup

    def _normalize_status_value(value: str | None, field_meta: dict[str, Any] | None) -> str | None:
        s = str(value or "").strip()
        if not s:
            return None
        id_to_name, _ = _field_option_maps(field_meta)
        return id_to_name.get(s, s)

    def _event_field_value_texts(raw: Any) -> list[str]:
        if isinstance(raw, str):
            s = raw.strip()
            if not s:
                return []
            try:
                parsed = json.loads(s)
            except Exception:
                return [s]
            return _field_texts(parsed)
        return _field_texts(raw)

    def _extract_previous_status_texts(payload: dict[str, Any], *, record_id: str, status_field_meta: dict[str, Any] | None) -> list[str]:
        fid = str(_field_meta_id(status_field_meta) or "").strip()
        if not fid:
            return []
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        action_list = event.get("action_list")
        if not isinstance(action_list, list):
            return []
        for action in action_list:
            if not isinstance(action, dict):
                continue
            rid = action.get("record_id") or action.get("recordId")
            if str(rid or "").strip() != str(record_id or "").strip():
                continue
            before_items = action.get("before_value")
            if not isinstance(before_items, list):
                continue
            for item in before_items:
                if not isinstance(item, dict):
                    continue
                item_fid = item.get("field_id") or item.get("fieldId")
                if str(item_fid or "").strip() != fid:
                    continue
                return _normalize_field_texts_for_meta(item.get("field_value") or item.get("fieldValue"), status_field_meta)
        return []

    def _should_trigger_cmd_text(cmd_text: str) -> bool:
        s = str(cmd_text or "").strip()
        if not s:
            return False
        if s.startswith("/"):
            return True
        if s.startswith("::/") or s.startswith("：：/") or s.startswith("::／") or s.startswith("：：／"):
            return True
        return False

    def _should_mark_record_running(cmd_name: str) -> bool:
        name0 = str(cmd_name or "").strip().lower()
        return name0 in ("run", "wf", "run_default")

    def _should_restore_previous_status(cmd_name: str) -> bool:
        name0 = str(cmd_name or "").strip().lower()
        # 这些指令会自己接管任务或队列的状态流转，这里不要再改回去，避免互相打架。
        if name0 in ("run", "wf", "run_default", "batch", "drain", "stop_queue"):
            return False
        # 其余命令按“工具/查询类”处理，执行完就把状态改回触发前的值。
        return True

    def _build_default_trigger_command(record_id: str) -> Any:
        return parse_message_text(f"/run record={str(record_id or '').strip()}")

    async def _try_trigger_record(*, table_key: str, record_id: str, operator_open_id: str | None, payload: dict[str, Any]) -> None:
        bitable = (ctx.bitables or {}).get(table_key)
        table_cfg = (ctx.bitable_configs or {}).get(table_key)
        if not bitable or not table_cfg:
            return
        if not getattr(bitable, "mode", None) or not getattr(bitable.mode, "read_enabled", False):
            return

        rec = await bitable.get_record(record_id)
        fields = rec.get("fields") if isinstance(rec, dict) and isinstance(rec.get("fields"), dict) else {}

        fields_cfg = dict(getattr(table_cfg, "fields", {}) or {})
        status_values_cfg = dict(getattr(table_cfg, "status_values", {}) or {})
        status_field = fields_cfg.get("status")
        status_field_meta = await _find_field_meta_by_name(bitable, status_field) if status_field else None
        trigger_value = _normalize_status_value(status_values_cfg.get("trigger") or "触发执行", status_field_meta) or "触发执行"
        queued_value = _normalize_status_value(status_values_cfg.get("queued"), status_field_meta)
        if status_field and trigger_value:
            st_val = fields.get(status_field)
            st_texts = _normalize_field_texts_for_meta(st_val, status_field_meta)
            if trigger_value not in st_texts:
                try:
                    logging.info(
                        "bitable_trigger skipped: status not trigger (table_key=%s record_id=%s status_field=%s trigger=%s got=%s)",
                        str(table_key),
                        str(record_id),
                        str(status_field),
                        str(trigger_value),
                        json.dumps(st_texts, ensure_ascii=False),
                    )
                except Exception:
                    pass
                return

        trig_field = _pick_trigger_field_name(table_cfg)
        if not trig_field:
            try:
                logging.info("bitable_trigger skipped: missing trigger field config (table_key=%s record_id=%s)", str(table_key), str(record_id))
            except Exception:
                pass
            return
        raw_cmd_val = fields.get(trig_field)
        cmd_texts = _field_texts(raw_cmd_val)
        cmd_text = cmd_texts[0] if cmd_texts else ""
        cmd = None
        if _should_trigger_cmd_text(cmd_text):
            cmd = parse_message_text(cmd_text)
            if not cmd:
                try:
                    logging.info(
                        "bitable_trigger skipped: cmd parse failed (table_key=%s record_id=%s cmd=%s)",
                        str(table_key),
                        str(record_id),
                        str(cmd_text or ""),
                    )
                except Exception:
                    pass
                return
        else:
            cmd = _build_default_trigger_command(record_id)
            cmd_text = f"/run record={str(record_id or '').strip()}"
            if not cmd:
                try:
                    keys_preview = ",".join(list(fields.keys())[:30]) if isinstance(fields, dict) else ""
                    logging.info(
                        "bitable_trigger skipped: cmd empty and default cmd build failed (table_key=%s record_id=%s cmd_field=%s field_keys=%s)",
                        str(table_key),
                        str(record_id),
                        str(trig_field),
                        keys_preview,
                    )
                except Exception:
                    pass
                return
            try:
                logging.info(
                    "bitable_trigger defaulted: cmd empty/invalid, fallback to %s (table_key=%s record_id=%s cmd_field=%s)",
                    str(cmd_text),
                    str(table_key),
                    str(record_id),
                    str(trig_field),
                )
            except Exception:
                pass

        op_open_id = str(operator_open_id or "").strip() or None
        if not op_open_id:
            user_field = _pick_trigger_user_field_name(table_cfg)
            if user_field:
                op_open_id = _extract_open_id_from_field(fields.get(user_field))
        if not op_open_id:
            try:
                logging.info(
                    "bitable_trigger skipped: missing operator open_id (table_key=%s record_id=%s cmd=%s)",
                    str(table_key),
                    str(record_id),
                    str(cmd_text or ""),
                )
            except Exception:
                pass
            return
        try:
            logging.info(
                "bitable_trigger accepted (table_key=%s record_id=%s cmd=%s name=%s args=%s user_open_id=%s)",
                str(table_key),
                str(record_id),
                str(cmd_text or ""),
                str(cmd.name or ""),
                json.dumps(cmd.args or {}, ensure_ascii=False),
                str(op_open_id or ""),
            )
        except Exception:
            pass

        should_mark_running = _should_mark_record_running(cmd.name)
        should_restore_previous = _should_restore_previous_status(cmd.name)
        restore_status_value: str | None = None
        if should_restore_previous and status_field:
            previous_status_texts = _extract_previous_status_texts(payload, record_id=record_id, status_field_meta=status_field_meta)
            restore_status_value = previous_status_texts[0] if previous_status_texts else (str(queued_value).strip() if isinstance(queued_value, str) and queued_value.strip() else None)
            try:
                logging.info(
                    "bitable_trigger previous status resolved (table_key=%s record_id=%s status_field=%s previous=%s fallback=%s final=%s)",
                    str(table_key),
                    str(record_id),
                    str(status_field),
                    json.dumps(previous_status_texts, ensure_ascii=False),
                    str(queued_value or ""),
                    str(restore_status_value or ""),
                )
            except Exception:
                pass

        if should_mark_running and getattr(bitable, "mode", None) and getattr(bitable.mode, "write_enabled", False):
            running_value = _normalize_status_value(status_values_cfg.get("running"), status_field_meta)
            if status_field and queued_value and running_value:
                try:
                    await bitable.update_record(record_id, {status_field: running_value})
                except Exception:
                    pass

        args = dict(cmd.args or {})
        if not any(k in args for k in ("record", "record_id", "recordId", "row", "row_no", "rowNo", "line")):
            args["record_id"] = record_id
        if not any(k in args for k in ("table", "tableKey", "table_key")):
            args["table"] = table_key
        trig = TriggerContext(chat_id=None, user_open_id=op_open_id, source="bitable.trigger")
        try:
            await dispatch(ctx, name=cmd.name, args=args, trigger=trig)
            if should_restore_previous and getattr(bitable, "mode", None) and getattr(bitable.mode, "write_enabled", False):
                if status_field and restore_status_value:
                    try:
                        await bitable.update_record(record_id, {status_field: restore_status_value})
                        logging.info(
                            "bitable_trigger restored previous status (table_key=%s record_id=%s status_field=%s restore=%s cmd=%s)",
                            str(table_key),
                            str(record_id),
                            str(status_field),
                            str(restore_status_value),
                            str(cmd_text or ""),
                        )
                    except Exception as e:
                        logging.warning(
                            "bitable_trigger restore previous status failed (table_key=%s record_id=%s status_field=%s restore=%s cmd=%s err=%s)",
                            str(table_key),
                            str(record_id),
                            str(status_field),
                            str(restore_status_value),
                            str(cmd_text or ""),
                            str(e),
                        )
        except Exception as e:
            try:
                logging.exception(
                    "bitable_trigger dispatch failed (table_key=%s record_id=%s cmd=%s user_open_id=%s): %s",
                    str(table_key),
                    str(record_id),
                    str(cmd_text or ""),
                    str(op_open_id or ""),
                    str(e),
                )
            except Exception:
                pass
            raise

    def handler(data: Any) -> None:
        payload = _normalize_payload(data)
        event_type = _extract_event_type(payload) or "unknown"
        operator_open_id = _extract_operator_open_id(payload)
        app_token, table_id = _extract_table_ref(payload)
        record_ids = _extract_record_ids(payload)
        record_id = record_ids[0] if record_ids else None
        table_key = _resolve_table_key(app_token, table_id)
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
                        await _try_trigger_record(table_key=table_key, record_id=rid, operator_open_id=operator_open_id, payload=payload)
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
            raise SystemExit(1)
        raise
    logging.basicConfig(level=getattr(logging, ctx.settings.bot_log_level.upper(), logging.INFO))
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

    cli = lark.ws.Client(ctx.settings.feishu_app_id, ctx.settings.feishu_app_secret, event_handler=event_handler, log_level=lark.LogLevel.INFO)
    cli.start()


if __name__ == "__main__":
    main()
