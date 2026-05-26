from __future__ import annotations

import json
import logging
import os
import threading
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
from .dispatcher import TriggerContext, dispatch_in_thread
from .feishu_auth import FeishuAuth
from .license_guard import check_license
from .ports import BitableConfig, BitableMode
from .queue_runner import QueueRunner
from .workflows import WorkflowRegistry

_BOT_OPEN_ID: str | None = None
_BOT_OPEN_ID_LOCK = threading.Lock()


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


def _collect_mention_ids(msg: Any, content: dict[str, Any]) -> list[str]:
    out: list[str] = []
    ms = getattr(msg, "mentions", None)
    if isinstance(ms, list):
        for it in ms:
            try:
                mid = getattr(it, "id", None)
                v = getattr(mid, "open_id", None) if mid else None
                if isinstance(v, str) and v.strip():
                    out.append(v.strip())
                    continue
                v2 = getattr(mid, "user_id", None) if mid else None
                if isinstance(v2, str) and v2.strip():
                    out.append(v2.strip())
            except Exception:
                continue

    raw = content.get("mentions")
    if isinstance(raw, list):
        for it in raw:
            if not isinstance(it, dict):
                continue
            id0 = it.get("id")
            if isinstance(id0, dict):
                for k in ("open_id", "openId", "user_id", "userId"):
                    v = id0.get(k)
                    if isinstance(v, str) and v.strip():
                        out.append(v.strip())
            for k in ("open_id", "openId", "user_id", "userId"):
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
    return {"queued": "待处理", "running": "执行中", "done": "已完成", "failed": "生成失败"}


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
            if isinstance(v, str):
                return v
    except Exception:
        pass
    return ""


def do_p2_im_message_receive_v1_factory(ctx: AppContext):
    def handler(data: P2ImMessageReceiveV1) -> None:
        msg = data.event.message
        content = _parse_message_content_json(msg.content)
        text = str(content.get("text") or "")
        if not text:
            text = _extract_text_from_message_content(msg.content)
        if text and not _should_accept_command_in_message(ctx, msg=msg, content=content, text=text):
            return
        cmd = parse_message_text(text)
        if not cmd:
            return
        chat_id = getattr(msg, "chat_id", None)
        sender = getattr(data.event, "sender", None)
        sender_id = getattr(sender, "sender_id", None) if sender else None
        user_open_id = getattr(sender_id, "open_id", None) if sender_id else None
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


def main() -> None:
    ctx = build_context()
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

    def _ignore_event(*_: Any, **__: Any) -> None:
        return

    builder = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1_factory(ctx))
        .register_p2_card_action_trigger(do_card_action_trigger_factory(ctx))
    )
    if hasattr(builder, "register_p2_im_message_message_read_v1"):
        getattr(builder, "register_p2_im_message_message_read_v1")(_ignore_event)
    else:
        if hasattr(builder, "register_p2_customized_event"):
            getattr(builder, "register_p2_customized_event")("im.message.message_read_v1", _ignore_event)
        builder.register_p1_customized_event("im.message.message_read_v1", _ignore_event)
    if hasattr(builder, "register_p2_customized_event"):
        getattr(builder, "register_p2_customized_event")("im.chat.access_event.bot_p2p_chat_entered_v1", _ignore_event)
    builder.register_p1_customized_event("im.chat.access_event.bot_p2p_chat_entered_v1", _ignore_event)

    event_handler = builder.build()

    cli = lark.ws.Client(ctx.settings.feishu_app_id, ctx.settings.feishu_app_secret, event_handler=event_handler, log_level=lark.LogLevel.INFO)
    cli.start()


if __name__ == "__main__":
    main()
