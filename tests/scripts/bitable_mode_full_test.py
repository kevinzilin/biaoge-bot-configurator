from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from biaoge_bot.callback_server import create_callback_app
from biaoge_bot.context import AppContext
from biaoge_bot.main import build_context as _main_build_context


def _build_context(*, env_file: str | None, workflow_config_path: str | None) -> AppContext:
    if workflow_config_path:
        os.environ["WORKFLOW_CONFIG_PATH"] = workflow_config_path
    ctx = _main_build_context(env_file)
    if hasattr(ctx.runner, "set_context"):
        ctx.runner.set_context(ctx)
    return ctx


async def _start_server(ctx: AppContext) -> tuple[uvicorn.Server, asyncio.Task[None], str]:
    app = create_callback_app(ctx)
    config = uvicorn.Config(
        app,
        host=ctx.settings.callback_host,
        port=ctx.settings.callback_port,
        log_level="info",
        access_log=True,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    base_url = f"http://{ctx.settings.callback_host}:{ctx.settings.callback_port}"
    for _ in range(100):
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                r = await client.get(f"{base_url}/healthz")
                if r.status_code == 200:
                    return server, task, base_url
        except Exception:
            pass
        await asyncio.sleep(0.1)
    raise RuntimeError("callback server failed to start")


async def _stop_server(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    server.should_exit = True
    try:
        await task
    except Exception:
        pass


async def _local_exec(base_url: str, text: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{base_url.rstrip('/')}/_local/exec", json={"text": text})
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            return data
        return {"ok": False, "error": "invalid response"}


def _history_is_done(item: dict[str, Any]) -> tuple[bool, str | None]:
    st = item.get("status")
    if isinstance(st, dict):
        status_str = st.get("status_str")
        completed = st.get("completed")
        if isinstance(status_str, str) and status_str.lower() in ("error", "failed", "failure"):
            return True, "failed"
        if completed is True:
            return True, None
    outputs = item.get("outputs")
    if isinstance(outputs, dict) and outputs:
        return True, None
    return False, None


async def _wait_comfyui_done(comfy: ComfyUIClient, *, prompt_id: str, timeout_s: int) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    last: dict[str, Any] | None = None
    while True:
        if time.time() > deadline:
            raise RuntimeError("timeout waiting comfyui history done")
        item = await comfy.get_history_item(prompt_id=prompt_id)
        if isinstance(item, dict):
            last = item
            done, _ = _history_is_done(item)
            if done:
                return item
        await asyncio.sleep(1)


async def _get_record(base_url: str, *, table: str, record_id: str) -> dict[str, Any] | None:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{base_url.rstrip('/')}/_local/bitable/record",
            params={"table": table, "record_id": record_id},
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("ok") and isinstance(data.get("record"), dict):
            return data["record"]
        return None


async def _wait_writeback(
    base_url: str,
    *,
    table: str,
    record_id: str,
    status_field: str,
    done_values: set[str],
    timeout_s: int,
) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    last: dict[str, Any] | None = None
    while True:
        if time.time() > deadline:
            raise RuntimeError("timeout waiting bitable writeback done")
        rec = await _get_record(base_url, table=table, record_id=record_id)
        if isinstance(rec, dict):
            last = rec
            fields = rec.get("fields")
            if isinstance(fields, dict):
                v = fields.get(status_field)
                if isinstance(v, str) and v in done_values:
                    return rec
        await asyncio.sleep(1)


async def _dump_debug(ctx: AppContext, *, prompt_id: str | None, base_url: str, table: str | None, record_id: str | None) -> None:
    print("\n--- DEBUG ---")
    if prompt_id:
        try:
            item = await ctx.comfyui.get_history_item(prompt_id=prompt_id)
            print("comfyui.history_item:", json.dumps(item, ensure_ascii=False)[:4000])
        except Exception as e:
            print("comfyui.history_item error:", str(e))
        try:
            q = await ctx.comfyui.get_queue()
            print("comfyui.queue:", json.dumps(q, ensure_ascii=False)[:4000])
        except Exception as e:
            print("comfyui.queue error:", str(e))
    if table and record_id:
        try:
            rec = await _get_record(base_url, table=table, record_id=record_id)
            print("bitable.record:", json.dumps(rec, ensure_ascii=False)[:4000])
        except Exception as e:
            print("bitable.record error:", str(e))
    print("--- DEBUG END ---\n")


async def _run_mode(
    *,
    mode: str,
    env_file: str | None,
    workflow_config_path: str | None,
    table_key: str,
    workflow_key: str,
    text: str,
    wait_writeback: bool,
    reset_first: bool,
    timeout_s: int,
) -> None:
    os.environ["BITABLE_MODE"] = mode
    ctx = _build_context(env_file=env_file, workflow_config_path=workflow_config_path)
    server, task, base_url = await _start_server(ctx)

    try:
        cfg = ctx.config or {}
        table_cfg = (cfg.get("tables") or {}).get(table_key) if isinstance(cfg.get("tables"), dict) else None
        status_field = None
        done_values: set[str] = set()
        if isinstance(table_cfg, dict):
            fields_cfg = table_cfg.get("fields")
            if isinstance(fields_cfg, dict):
                status_field = fields_cfg.get("status")
            status_values = table_cfg.get("status_values")
            if isinstance(status_values, dict):
                done = status_values.get("done")
                failed = status_values.get("failed")
                if isinstance(done, str) and done:
                    done_values.add(done)
                if isinstance(failed, str) and failed:
                    done_values.add(failed)

        print(f"\n==================== MODE: {mode} ====================")
        if reset_first:
            res = await _local_exec(base_url, f"/reset table={table_key} scope=all clear=1")
            print("reset:", json.dumps(res, ensure_ascii=False))

        res = await _local_exec(base_url, text)
        print("exec:", json.dumps(res, ensure_ascii=False))

        if not res.get("ok"):
            return
        prompt_id = res.get("prompt_id")
        prompt_id = str(prompt_id) if isinstance(prompt_id, str) and prompt_id else None
        record_id = res.get("record_id")
        record_id = str(record_id) if isinstance(record_id, str) and record_id else None

        if prompt_id:
            await _wait_comfyui_done(ctx.comfyui, prompt_id=prompt_id, timeout_s=timeout_s)
            print("comfyui: done", prompt_id)

        if wait_writeback and record_id and status_field and done_values:
            rec = await _wait_writeback(
                base_url,
                table=table_key,
                record_id=record_id,
                status_field=str(status_field),
                done_values=done_values,
                timeout_s=timeout_s,
            )
            fields = rec.get("fields") if isinstance(rec.get("fields"), dict) else {}
            print(
                "writeback:",
                json.dumps(
                    {
                        "status": fields.get(status_field),
                        "prompt_id": fields.get(((table_cfg or {}).get("fields") or {}).get("prompt_id", "任务ID")),
                        "output": fields.get(((table_cfg or {}).get("fields") or {}).get("output", "生成结果")),
                        "error": fields.get(((table_cfg or {}).get("fields") or {}).get("error", "错误信息")),
                    },
                    ensure_ascii=False,
                ),
            )
        elif record_id and status_field:
            rec = await _get_record(base_url, table=table_key, record_id=record_id)
            if isinstance(rec, dict):
                fields = rec.get("fields") if isinstance(rec.get("fields"), dict) else {}
                print(
                    "record:",
                    json.dumps(
                        {
                            "status": fields.get(status_field),
                            "prompt_id": fields.get(((table_cfg or {}).get("fields") or {}).get("prompt_id", "任务ID")),
                            "output": fields.get(((table_cfg or {}).get("fields") or {}).get("output", "生成结果")),
                            "error": fields.get(((table_cfg or {}).get("fields") or {}).get("error", "错误信息")),
                        },
                        ensure_ascii=False,
                    ),
                )
    except Exception as e:
        try:
            await _dump_debug(
                ctx,
                prompt_id=str(res.get("prompt_id") or "") if isinstance(res, dict) else None,
                base_url=base_url,
                table=table_key,
                record_id=str(res.get("record_id") or "") if isinstance(res, dict) else None,
            )
        except Exception:
            pass
        raise e
    finally:
        await _stop_server(server, task)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--env", dest="env_file", default=None)
    p.add_argument("--config", dest="workflow_config_path", default=None)
    p.add_argument("--table", dest="table_key", default="klein_table")
    p.add_argument("--workflow", dest="workflow_key", default="klein_add_real_details")
    p.add_argument("--row", type=int, default=2)
    p.add_argument("--timeout", type=int, default=1800)
    args = p.parse_args()

    base_row = int(args.row)
    run_auto = f"/wf {args.workflow_key} row={base_row} 449.text=BITABLE_MODE_auto 448.image=1.jpg 481.image=1.jpg"
    run_read = f"/wf {args.workflow_key} row={base_row + 1} 449.text=BITABLE_MODE_read 448.image=1.jpg 481.image=1.jpg"
    run_write = f"/wf {args.workflow_key} row={base_row + 2} 449.text=BITABLE_MODE_write 448.image=1.jpg 481.image=1.jpg"
    run_off = f"/wf {args.workflow_key} 449.text=BITABLE_MODE_off 448.image=1.jpg 481.image=1.jpg"

    async def _run() -> None:
        await _run_mode(
            mode="auto",
            env_file=args.env_file,
            workflow_config_path=args.workflow_config_path,
            table_key=str(args.table_key),
            workflow_key=str(args.workflow_key),
            text=run_auto,
            wait_writeback=True,
            reset_first=True,
            timeout_s=int(args.timeout),
        )
        await _run_mode(
            mode="read",
            env_file=args.env_file,
            workflow_config_path=args.workflow_config_path,
            table_key=str(args.table_key),
            workflow_key=str(args.workflow_key),
            text=run_read,
            wait_writeback=False,
            reset_first=False,
            timeout_s=int(args.timeout),
        )
        await _run_mode(
            mode="write",
            env_file=args.env_file,
            workflow_config_path=args.workflow_config_path,
            table_key=str(args.table_key),
            workflow_key=str(args.workflow_key),
            text=run_write,
            wait_writeback=True,
            reset_first=False,
            timeout_s=int(args.timeout),
        )
        await _run_mode(
            mode="off",
            env_file=args.env_file,
            workflow_config_path=args.workflow_config_path,
            table_key=str(args.table_key),
            workflow_key=str(args.workflow_key),
            text=run_off,
            wait_writeback=False,
            reset_first=False,
            timeout_s=int(args.timeout),
        )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
