from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import httpx


async def call(base_url: str, text: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{base_url.rstrip('/')}/_local/exec", json={"text": text})
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            return data
        return {"ok": False, "error": "invalid response"}


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://192.168.0.22:9901")
    p.add_argument("--table", default="klein_table")
    p.add_argument("--workflow", default="klein_add_real_details")
    p.add_argument("--run-drain", action="store_true")
    args = p.parse_args()

    cmds = [
        "/help",
        "/h",
        "/panel",
        f"/reset table={args.table} scope=all clear=1",
        "/run_default",
        "/run",
        "/run row=2",
        f"/run table={args.table} row=3",
        f"/wf {args.workflow}",
        f"/wf {args.workflow} table={args.table} row=4",
        f"/wf {args.workflow} row=5 448.image=1.jpg",
        "/stop_queue",
        f"/stop_queue {args.workflow} table={args.table}",
        "/stop_queue",
    ]
    if args.run_drain:
        cmds.append("/batch")
        cmds.append(f"/batch {args.workflow}")
        cmds.append(f"/batch {args.workflow} table={args.table} batch=1 inflight=1")
        cmds.append("/drain")
        cmds.append(f"/drain {args.workflow} table={args.table} batch=2 inflight=1")

    for c in cmds:
        print(f"\n=========================================")
        print(f"Testing Command: {c}")
        print(f"=========================================")
        res = await call(args.base_url, c)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        await asyncio.sleep(0.5)

if __name__ == "__main__":
    asyncio.run(main())
