from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from .dispatcher import TriggerContext, run_workflow


@dataclass
class _RunState:
    workflow_key: str
    table_key: str
    batch: int
    inflight_limit: int
    drain: bool
    active: bool
    inflight: int
    chat_id: str | None
    prompt_to_record: dict[str, str]
    fetched_once: bool


class QueueRunner:
    def __init__(self) -> None:
        self._ctx: Any = None
        self._lock = asyncio.Lock()
        self._runs: dict[str, _RunState] = {}
        self._prompt_to_run_key: dict[str, str] = {}
        self._prompt_temp_files: dict[str, list[str]] = {}
        self._prompt_ctx: dict[str, dict[str, Any]] = {}
        self._pending_remote: dict[str, dict[str, Any]] = {}
        self._poller_started = False
        self._im_attach_lock = threading.Lock()
        self._im_attachments: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def set_context(self, ctx: Any) -> None:
        self._ctx = ctx

    def register_im_attachment(
        self,
        *,
        chat_id: str | None,
        user_open_id: str | None,
        kind: str,
        key: str,
        file_name: str | None = None,
        message_id: str | None = None,
    ) -> None:
        cid = str(chat_id or "").strip()
        uid = str(user_open_id or "").strip()
        k0 = str(kind or "").strip().lower()
        key0 = str(key or "").strip()
        if not cid or not uid or not key0:
            return
        item: dict[str, Any] = {"kind": k0, "key": key0, "file_name": str(file_name or "").strip() or None, "message_id": str(message_id or "").strip() or None, "ts": time.time()}
        with self._im_attach_lock:
            lst = list(self._im_attachments.get((cid, uid), []))
            lst.append(item)
            lst = [x for x in lst if isinstance(x, dict)]
            lst = lst[-10:]
            self._im_attachments[(cid, uid)] = lst

    def get_im_attachment(self, *, chat_id: str | None, user_open_id: str | None, selector: str) -> dict[str, Any] | None:
        cid = str(chat_id or "").strip()
        uid = str(user_open_id or "").strip()
        if not cid or not uid:
            return None
        sel = str(selector or "").strip().lower()
        nth = 1
        if sel.startswith("last:"):
            try:
                nth = int(sel.split(":", 1)[1])
            except Exception:
                nth = 1
        nth = max(1, nth)
        with self._im_attach_lock:
            lst = list(self._im_attachments.get((cid, uid), []))
        if not lst:
            return None
        if nth > len(lst):
            return None
        return lst[-nth]

    def start_remote_poller(self) -> None:
        if self._poller_started:
            return
        self._poller_started = True
        threading.Thread(target=self._poller_thread_main, daemon=True).start()

    def _poller_thread_main(self) -> None:
        try:
            asyncio.run(self._poll_remote_loop())
        except Exception:
            pass

    async def _poll_remote_loop(self) -> None:
        while True:
            ctx = self._ctx
            interval = 60
            mode = "poll"
            fallback = 600
            try:
                settings = getattr(ctx, "settings", None) if ctx else None
                interval = int(getattr(settings, "remote_poll_interval_seconds", interval) or interval) if settings else interval
                interval = max(10, interval)
                mode = str(getattr(settings, "remote_result_mode", mode) or mode).strip().lower() if settings else mode
                fallback = int(getattr(settings, "remote_poll_fallback_seconds", fallback) or fallback) if settings else fallback
                fallback = max(0, fallback)
            except Exception:
                pass
            await asyncio.sleep(interval)
            if not ctx:
                continue
            async with self._lock:
                pending = list(self._pending_remote.items())
            if not pending:
                continue
            for pid, info in pending[:50]:
                if mode != "poll" and fallback > 0:
                    created_at = info.get("created_at")
                    try:
                        created_at = float(created_at) if created_at is not None else 0.0
                    except Exception:
                        created_at = 0.0
                    if created_at > 0 and (time.time() - created_at) < float(fallback):
                        continue
                provider = str(info.get("provider") or "").strip().lower()
                payload: dict[str, Any] | None = None
                try:
                    if provider == "runninghub":
                        from .runninghub import RunningHubClient

                        cli = RunningHubClient(api_key=str(getattr(getattr(ctx, "settings", None), "runninghub_api_key", None) or ""))
                        q = await cli.query_results_v2(task_id=pid)
                        st = (q.status or "").strip().upper()
                        if st not in ("SUCCESS", "SUCCEEDED", "OK", "FAILED", "FAILURE", "ERROR"):
                            continue
                        ok = st in ("SUCCESS", "SUCCEEDED", "OK")
                        payload = {"provider": "runninghub", "prompt_id": pid, "completed": True, "status": "success" if ok else "failed"}
                        if not ok:
                            if q.error_message:
                                payload["errorMessage"] = q.error_message
                            if q.error_code:
                                payload["errorCode"] = q.error_code
                        files: list[dict[str, Any]] = []
                        for it in q.results:
                            url = it.get("url")
                            if isinstance(url, str) and url.strip():
                                files.append({"url": url.strip(), "outputType": it.get("outputType")})
                        if files:
                            payload["files"] = files
                    elif provider in ("comfyui", ""):
                        from .comfyui import ComfyUIClient

                        base_url = str(info.get("comfyui_base_url") or "").strip() or str(getattr(getattr(ctx, "settings", None), "comfyui_base_url", "") or "")
                        if not base_url:
                            continue
                        cli = ComfyUIClient(base_url)
                        item = await cli.get_history_item(prompt_id=pid)
                        if not isinstance(item, dict):
                            continue
                        payload = {
                            "provider": "comfyui",
                            "prompt_id": pid,
                            "completed": True,
                            "status": "success",
                            "context": {"comfyui_base_url": base_url},
                            "result": item,
                        }
                    else:
                        continue
                except Exception:
                    continue

                if not payload:
                    continue
                try:
                    from .callback_server import handle_callback_payload

                    await handle_callback_payload(ctx, payload)
                except Exception:
                    pass
                async with self._lock:
                    self._pending_remote.pop(pid, None)

    def _run_key(self, table_key: str, workflow_key: str) -> str:
        return f"{table_key}::{workflow_key}"

    async def start(
        self,
        *,
        workflow_key: str,
        table_key: str,
        batch: int,
        inflight: int,
        drain: bool,
        chat_id: str | None,
    ) -> None:
        async with self._lock:
            if not self._ctx:
                raise RuntimeError("runner not initialized")
            b = max(1, min(int(batch), 200))
            i = max(1, min(int(inflight), 8))
            rk = self._run_key(table_key, workflow_key)
            self._runs[rk] = _RunState(
                workflow_key=workflow_key,
                table_key=table_key,
                batch=b,
                inflight_limit=i,
                drain=bool(drain),
                active=True,
                inflight=0,
                chat_id=chat_id,
                prompt_to_record={},
                fetched_once=False,
            )
        await self._fill(rk)

    async def stop(self, *, workflow_key: str, table_key: str) -> None:
        async with self._lock:
            rk = self._run_key(table_key, workflow_key)
            st = self._runs.get(rk)
            if st:
                st.active = False

    async def on_done(self, *, prompt_id: str) -> None:
        temp_files: list[str] = []
        async with self._lock:
            temp_files = self._prompt_temp_files.pop(prompt_id, [])
            self._prompt_ctx.pop(prompt_id, None)
            self._pending_remote.pop(prompt_id, None)
            rk = self._prompt_to_run_key.pop(prompt_id, None)
            st = self._runs.get(rk) if rk else None
            if st:
                st.inflight = max(0, st.inflight - 1)
                st.prompt_to_record.pop(prompt_id, None)

        if temp_files and self._ctx:
            base_dir = str(getattr(getattr(self._ctx, "settings", None), "bitable_download_dir", "") or "").strip()
            if base_dir:
                base_dir_abs = os.path.abspath(base_dir)
                for fp in temp_files:
                    try:
                        if not fp:
                            continue
                        ap = os.path.abspath(fp)
                        if not ap.startswith(base_dir_abs):
                            continue
                        if os.path.exists(ap):
                            os.remove(ap)
                    except Exception:
                        pass

        if st and rk:
            await self._fill(rk)

    async def register_temp_files(self, *, prompt_id: str, file_paths: list[str]) -> None:
        pid = str(prompt_id or "").strip()
        if not pid:
            return
        paths = [str(x) for x in (file_paths or []) if x]
        if not paths:
            return
        async with self._lock:
            cur = self._prompt_temp_files.get(pid) or []
            seen = set(cur)
            for p in paths:
                if p not in seen:
                    cur.append(p)
                    seen.add(p)
            self._prompt_temp_files[pid] = cur

    async def register_prompt_context(
        self,
        *,
        prompt_id: str,
        record_id: str | None,
        table_key: str | None,
        workflow_key: str | None,
        chat_id: str | None,
    ) -> None:
        pid = str(prompt_id or "").strip()
        if not pid:
            return
        async with self._lock:
            self._prompt_ctx[pid] = {
                "record_id": record_id,
                "table_key": table_key,
                "workflow_key": workflow_key,
                "chat_id": chat_id,
            }

    async def register_pending_remote(self, *, prompt_id: str, provider: str, comfyui_base_url: str | None = None) -> None:
        pid = str(prompt_id or "").strip()
        if not pid:
            return
        async with self._lock:
            self._pending_remote[pid] = {
                "provider": str(provider or "").strip().lower(),
                "comfyui_base_url": comfyui_base_url,
                "created_at": time.time(),
            }

    async def resolve_prompt(self, *, prompt_id: str) -> dict[str, Any] | None:
        async with self._lock:
            c = self._prompt_ctx.get(prompt_id)
            if isinstance(c, dict):
                return {
                    "record_id": c.get("record_id"),
                    "table_key": c.get("table_key"),
                    "workflow_key": c.get("workflow_key"),
                    "chat_id": c.get("chat_id"),
                }
            rk = self._prompt_to_run_key.get(prompt_id)
            if not rk:
                return None
            st = self._runs.get(rk)
            if not st:
                return None
            record_id = st.prompt_to_record.get(prompt_id)
            if not record_id:
                return None
            return {
                "record_id": record_id,
                "table_key": st.table_key,
                "workflow_key": st.workflow_key,
                "chat_id": st.chat_id,
            }

    async def _fill(self, rk: str) -> None:
        async with self._lock:
            st = self._runs.get(rk)
            if not st or not st.active:
                return
            if not st.drain and st.fetched_once:
                return
            ctx = self._ctx
            bitable = ctx.bitables.get(st.table_key)
            table_cfg = ctx.bitable_configs.get(st.table_key)
            if not bitable or not table_cfg:
                st.active = False
                return
            if not bitable.mode.write_enabled:
                st.active = False
                return

            needed = st.inflight_limit - st.inflight
            if needed <= 0:
                return

            status_field = table_cfg.fields.get("status")
            queued_value = table_cfg.status_values.get("queued")
            running_value = table_cfg.status_values.get("running")
            created_time_field = table_cfg.fields.get("created_time")
            if not status_field or not queued_value or not running_value:
                st.active = False
                return

        ids = await self._claim_records(
            rk=rk,
            limit=min(needed, st.batch),
            status_field=status_field,
            queued_value=queued_value,
            running_value=running_value,
            sort_field=created_time_field,
        )
        async with self._lock:
            st3 = self._runs.get(rk)
            if st3:
                st3.fetched_once = True
        if not ids:
            async with self._lock:
                st2 = self._runs.get(rk)
                if st2 and st2.active and st2.inflight == 0:
                    st2.active = False
            return


        for record_id in ids:
            await self._queue_one(rk=rk, record_id=record_id)

        await self._fill(rk)

    async def _claim_records(
        self,
        *,
        rk: str,
        limit: int,
        status_field: str,
        queued_value: str,
        running_value: str,
        sort_field: str | None,
    ) -> list[str]:
        async with self._lock:
            st = self._runs.get(rk)
            ctx = self._ctx
            bitable = ctx.bitables.get(st.table_key) if st else None
        if not st or not st.active or not bitable:
            return []

        filter_ = {
            "conjunction": "and",
            "conditions": [{"field_name": status_field, "operator": "is", "value": [queued_value]}],
        }
        sort = [{"field_name": sort_field, "desc": False}] if sort_field else None
        items = await bitable.search_records(filter_=filter_, sort=sort, page_size=limit)
        out: list[str] = []
        for it in items:
            record_id = it.get("record_id")
            if not record_id:
                continue
            try:
                await bitable.update_record(str(record_id), {status_field: running_value})
            except Exception:
                continue
            out.append(str(record_id))
        return out

    async def _queue_one(self, *, rk: str, record_id: str) -> None:
        async with self._lock:
            st = self._runs.get(rk)
            if not st or not st.active:
                return
            ctx = self._ctx
            st.inflight += 1
            chat_id = st.chat_id

        prompt_id: str | None = None
        err: str | None = None
        try:
            prompt_id = await run_workflow(
                ctx,
                trigger=TriggerContext(chat_id=chat_id, user_open_id=None, source="queue_runner"),
                workflow_key=st.workflow_key,
                record_id=record_id,
                row=None,
                view_id=None,
                params={},
                table_key=st.table_key,
            )
        except Exception as e:
            err = str(e)

        async with self._lock:
            st2 = self._runs.get(rk)
            ctx = self._ctx
            bitable = ctx.bitables.get(st2.table_key) if st2 else None
            table_cfg = ctx.bitable_configs.get(st2.table_key) if st2 else None
            if st2:
                if prompt_id:
                    st2.prompt_to_record[prompt_id] = record_id
                    self._prompt_to_run_key[prompt_id] = rk
                else:
                    st2.inflight = max(0, st2.inflight - 1)

        if prompt_id and bitable and table_cfg and bitable.mode.write_enabled:
            prompt_field = table_cfg.fields.get("prompt_id")
            if prompt_field:
                try:
                    await bitable.update_record(record_id, {prompt_field: prompt_id})
                except Exception:
                    pass

        if not prompt_id and bitable and table_cfg and bitable.mode.write_enabled:
            status_field = table_cfg.fields.get("status")
            failed_value = table_cfg.status_values.get("failed")
            error_field = table_cfg.fields.get("error")
            fields: dict[str, Any] = {}
            if status_field and failed_value:
                fields[status_field] = failed_value
            if error_field:
                fields[error_field] = err or "queue failed"
            if fields:
                try:
                    await bitable.update_record(record_id, fields)
                except Exception:
                    pass
