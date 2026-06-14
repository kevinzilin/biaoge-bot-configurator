from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import uvicorn

from biaoge_bot.callback_server import create_callback_app
from biaoge_bot.comfyui import ComfyUIClient
from biaoge_bot.config import load_json_with_env, load_settings
from biaoge_bot.context import AppContext
from biaoge_bot.feishu_auth import FeishuAuth
from biaoge_bot.main import _parse_bitable_mode, _parse_tables
from biaoge_bot.ports import BitableMode
from biaoge_bot.queue_runner import QueueRunner
from biaoge_bot.workflows import WorkflowRegistry


def _build_context(*, env_file: str | None, workflow_config_path: str | None) -> AppContext:
    if workflow_config_path:
        os.environ["WORKFLOW_CONFIG_PATH"] = workflow_config_path

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

    drive = None
    bitables: dict[str, Any] = {}
    if tables and (bitable_mode.read_enabled or bitable_mode.write_enabled):
        from biaoge_bot.modules.bitable import BitableClient
        from biaoge_bot.modules.drive import DriveClient

        for key, table_cfg in tables.items():
            bitables[key] = BitableClient(auth, table_cfg, bitable_mode)
        drive = DriveClient(auth)

    comfyui = ComfyUIClient(settings.comfyui_base_url)
    workflows = WorkflowRegistry.from_config(cfg)
    runner = QueueRunner()
    ctx = AppContext(
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
        runner=runner,
    )
    if hasattr(ctx.runner, "set_context"):
        ctx.runner.set_context(ctx)
    return ctx


def _status_values_for_reset(cfg: Any, scope: str) -> set[str]:
    status_values = getattr(cfg, "status_values", None) or {}
    if not isinstance(status_values, dict):
        return set()

    if scope == "all_nonqueued":
        out = {str(v) for k, v in status_values.items() if k != "queued" and v}
        return {x for x in out if x}

    keys: Iterable[str]
    if scope == "running_failed":
        keys = ("running", "failed")
    elif scope == "failed_only":
        keys = ("failed",)
    else:
        keys = ()

    out2 = {str(status_values.get(k) or "") for k in keys}
    return {x for x in out2 if x}


async def reset_table_status(
    ctx: AppContext,
    *,
    table_key: str,
    scope: str,
    clear: bool,
) -> int:
    bitable = ctx.bitables.get(table_key)
    table_cfg = ctx.bitable_configs.get(table_key)
    if not bitable or not table_cfg:
        raise RuntimeError(f"table not found: {table_key}")
    if not bitable.mode.read_enabled or not bitable.mode.write_enabled:
        raise RuntimeError("bitable read/write disabled")

    status_field = table_cfg.fields.get("status")
    queued_value = table_cfg.status_values.get("queued")
    if not status_field or not queued_value:
        raise RuntimeError("missing status field/status_values.queued")

    targets = _status_values_for_reset(table_cfg, scope)
    if not targets:
        return 0

    output_field = table_cfg.fields.get("output")
    error_field = table_cfg.fields.get("error")
    prompt_field = table_cfg.fields.get("prompt_id")

    changed = 0
    cleared_prompt = 0
    cleared_error = 0
    cleared_output = 0
    failed_clear = 0
    page_token: str | None = None
    while True:
        items, page_token, has_more, _ = await bitable.list_records_page(view_id=table_cfg.view_id, page_size=200, page_token=page_token)
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
                ok_this = True
                if prompt_field:
                    ok = False
                    for v in ("", None):
                        try:
                            await bitable.update_record(str(rid), {prompt_field: v})
                            ok = True
                            break
                        except Exception:
                            continue
                    if ok:
                        cleared_prompt += 1
                    else:
                        ok_this = False
                if error_field:
                    ok = False
                    for v in ("", None):
                        try:
                            await bitable.update_record(str(rid), {error_field: v})
                            ok = True
                            break
                        except Exception:
                            continue
                    if ok:
                        cleared_error += 1
                    else:
                        ok_this = False
                if output_field:
                    ok = False
                    for v in ([], None, ""):
                        try:
                            await bitable.update_record(str(rid), {output_field: v})
                            ok = True
                            break
                        except Exception:
                            continue
                    if ok:
                        cleared_output += 1
                    else:
                        ok_this = False
                if not ok_this:
                    failed_clear += 1
            changed += 1

        if not has_more or not page_token:
            break

    print(f"reset cleared: prompt={cleared_prompt} error={cleared_error} output={cleared_output} failed={failed_clear}")
    return changed


async def wait_queue_runner_done(
    ctx: AppContext,
    *,
    workflow_key: str,
    table_key: str,
    timeout_s: int,
) -> None:
    rk = f"{table_key}::{workflow_key}"
    deadline = (time.time() + timeout_s) if timeout_s > 0 else None

    while True:
        if deadline and time.time() > deadline:
            raise RuntimeError(f"timeout waiting runner done: {rk}")

        async with ctx.runner._lock:
            st = ctx.runner._runs.get(rk)
            active = bool(getattr(st, "active", False)) if st else False
            inflight = int(getattr(st, "inflight", 0)) if st else 0

        if (not st) or (not active and inflight <= 0):
            return

        await asyncio.sleep(1)


async def run_drain(
    ctx: AppContext,
    *,
    workflow_key: str,
    table_key: str,
    batch: int,
    inflight: int,
    timeout_s: int,
) -> None:
    await ctx.runner.start(
        workflow_key=workflow_key,
        table_key=table_key,
        batch=batch,
        inflight=inflight,
        drain=True,
        chat_id=None,
    )
    await wait_queue_runner_done(ctx, workflow_key=workflow_key, table_key=table_key, timeout_s=timeout_s)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--env", dest="env_file", default=None)
    p.add_argument("--config", dest="workflow_config_path", default=None)
    p.add_argument("--workflow", dest="workflow_key", required=True)
    p.add_argument("--table", dest="table_key", default=None)
    p.add_argument("--reset", action="store_true")
    p.add_argument("--run", action="store_true")
    p.add_argument("--reset-scope", dest="reset_scope", default="all_nonqueued", choices=("all_nonqueued", "running_failed", "failed_only"))
    p.add_argument("--clear", action="store_true")
    p.add_argument("--batch", type=int, default=10)
    p.add_argument("--inflight", type=int, default=1)
    p.add_argument("--timeout", type=int, default=36000)
    args = p.parse_args()

    do_reset = bool(args.reset)
    do_run = bool(args.run)
    if not do_reset and not do_run:
        do_reset = True
        do_run = True

    ctx = _build_context(env_file=args.env_file, workflow_config_path=args.workflow_config_path)
    table_key = str(args.table_key or "")
    if not table_key:
        raw_wf = (ctx.config.get("workflows") or {}).get(str(args.workflow_key) or "")
        if isinstance(raw_wf, dict):
            tk = raw_wf.get("table")
            if isinstance(tk, str) and tk.strip():
                table_key = tk.strip()
    if not table_key:
        table_key = str(ctx.default_table_key or "")
    if not table_key:
        raise RuntimeError("missing table_key")

    async def _run() -> None:
        app = create_callback_app(ctx)
        config = uvicorn.Config(
            app,
            host=ctx.settings.callback_host,
            port=ctx.settings.callback_port,
            log_level="info",
            access_log=True,
        )
        server = uvicorn.Server(config)
        serve_task = asyncio.create_task(server.serve())
        await asyncio.sleep(0.2)
        if do_reset:
            n = await reset_table_status(ctx, table_key=table_key, scope=args.reset_scope, clear=bool(args.clear))
            print(f"reset changed: {n}")
        if do_run:
            await run_drain(
                ctx,
                workflow_key=str(args.workflow_key),
                table_key=table_key,
                batch=int(args.batch),
                inflight=int(args.inflight),
                timeout_s=int(args.timeout),
            )
            print("drain done")
        server.should_exit = True
        await serve_task

    asyncio.run(_run())


if __name__ == "__main__":
    main()
