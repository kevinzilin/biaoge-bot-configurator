from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from .dispatcher import TriggerContext, run_workflow
from .modules.bitable_logic import claim_records as _enc_claim_records

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
    remaining: int | None
    empty_claims: int


@dataclass
class _RecordRunState:
    planned_total: int
    submitted_total: int | None
    pending: set[str]
    done: set[str]


class QueueRunner:
    def __init__(self) -> None:
        self._ctx: Any = None
        self._lock = asyncio.Lock()
        self._runs: dict[str, _RunState] = {}
        self._prompt_to_run_key: dict[str, str] = {}
        self._prompt_temp_files: dict[str, list[str]] = {}
        self._prompt_ctx: dict[str, dict[str, Any]] = {}
        self._pending_remote: dict[str, dict[str, Any]] = {}
        self._ignored_prompt_ids: dict[str, float] = {}
        self._record_runs: dict[tuple[str, str], _RecordRunState] = {}
        self._filling: set[str] = set()
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
            for k in ((cid, uid), (cid, "")):
                lst = list(self._im_attachments.get(k, []))
                lst.append(item)
                lst = [x for x in lst if isinstance(x, dict)]
                lst = lst[-10:]
                self._im_attachments[k] = lst

    def get_im_attachment(self, *, chat_id: str | None, user_open_id: str | None, selector: str) -> dict[str, Any] | None:
        cid = str(chat_id or "").strip()
        uid = str(user_open_id or "").strip()
        if not cid:
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
            lst: list[dict[str, Any]] = []
            if uid:
                lst = list(self._im_attachments.get((cid, uid), []))
            if not lst:
                lst = list(self._im_attachments.get((cid, ""), []))
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
        while True:
            try:
                asyncio.run(self._poll_remote_loop())
            except Exception:
                logging.exception("poller: remote loop crashed; restarting in 10s")
                time.sleep(10)

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

            # 一次取完所有 pending 项（上限 100），按提交时间排序让先提交的先处理
            sorted_pending = sorted(pending, key=lambda kv: float(kv[1].get("created_at") or 0.0) if isinstance(kv[1], dict) else 0.0)
            sorted_pending = sorted_pending[:100]

            # 超时检查：超过 _pending_timeout_seconds 的任务强制标为失败
            _PENDING_TIMEOUT = float(getattr(getattr(ctx, "settings", None), "pending_timeout_seconds", 0) or 0) or 7200
            if _PENDING_TIMEOUT > 0:
                now_ts = time.time()
                timed_out: list[str] = []
                for pid, info in sorted_pending:
                    created_at = info.get("created_at") if isinstance(info, dict) else None
                    try:
                        created_at = float(created_at) if created_at is not None else 0.0
                    except Exception:
                        created_at = 0.0
                    if created_at > 0 and (now_ts - created_at) > _PENDING_TIMEOUT:
                        timed_out.append(pid)
                for pid in timed_out:
                    logging.warning("poller: task %s timed out after %ss, forcing failed status", pid, int(_PENDING_TIMEOUT))
                    try:
                        cctx = None
                        async with self._lock:
                            c = self._prompt_ctx.get(pid)
                            if isinstance(c, dict):
                                cctx = dict(c)
                        if cctx:
                            tk = str(cctx.get("table_key") or "").strip()
                            rid = str(cctx.get("record_id") or "").strip()
                            if tk and rid:
                                bitable = ctx.bitables.get(tk) if ctx.bitables else None
                                tcfg = ctx.bitable_configs.get(tk) if ctx.bitable_configs else None
                                if bitable and tcfg and getattr(bitable, "mode", None) and getattr(bitable.mode, "write_enabled", False):
                                    status_field = tcfg.fields.get("status")
                                    failed_value = tcfg.status_values.get("failed")
                                    if status_field and failed_value:
                                        await bitable.update_record(rid, {status_field: failed_value})
                                    error_field = tcfg.fields.get("error")
                                    if error_field:
                                        await bitable.update_record(rid, {error_field: f"任务超时（超过{int(_PENDING_TIMEOUT)}秒未完成）"})
                    except Exception:
                        logging.exception("poller: failed to write timeout failure status for prompt_id=%s", pid)
                    async with self._lock:
                        self._pending_remote.pop(pid, None)
                        self._prompt_ctx.pop(pid, None)

            # 第一步：并发查询所有任务的状态，快速识别哪些已经完成
            async def _query_one(pid: str, info: dict[str, Any]) -> dict[str, Any] | None:
                if mode != "poll" and fallback > 0:
                    created_at = info.get("created_at")
                    try:
                        created_at = float(created_at) if created_at is not None else 0.0
                    except Exception:
                        created_at = 0.0
                    if created_at > 0 and (time.time() - created_at) < float(fallback):
                        return None
                provider = str(info.get("provider") or "").strip().lower()
                try:
                    if provider == "runninghub":
                        from .runninghub import RunningHubClient

                        cli = RunningHubClient(api_key=str(getattr(getattr(ctx, "settings", None), "runninghub_api_key", None) or ""))
                        q = await cli.query_results_v2(task_id=pid)
                        st = (q.status or "").strip().upper()
                        if st not in ("SUCCESS", "SUCCEEDED", "OK", "FAILED", "FAILURE", "ERROR"):
                            return None
                        ok = st in ("SUCCESS", "SUCCEEDED", "OK")
                        payload: dict[str, Any] = {"provider": "runninghub", "prompt_id": pid, "completed": True, "status": "success" if ok else "failed"}
                        if not ok:
                            if q.error_message:
                                payload["errorMessage"] = q.error_message
                            if q.error_code:
                                payload["errorCode"] = q.error_code
                        rh_files: list[dict[str, Any]] = []
                        for it in q.results:
                            url = it.get("url")
                            if isinstance(url, str) and url.strip():
                                rh_files.append({"url": url.strip(), "outputType": it.get("outputType")})
                        if rh_files:
                            payload["files"] = rh_files
                        # 附加上下文
                        cctx: dict[str, Any] | None = None
                        async with self._lock:
                            c = self._prompt_ctx.get(pid)
                            if isinstance(c, dict):
                                tk = str(c.get("table_key") or "").strip()
                                cctx = {
                                    "record_id": c.get("record_id"),
                                    "recordId": c.get("record_id"),
                                    "workflow": c.get("workflow_key"),
                                    "tableKey": tk,
                                    "chat_id": c.get("chat_id"),
                                    "user_open_id": c.get("user_open_id"),
                                    "runLogTableKey": c.get("run_log_table_key"),
                                    "runLogRecordId": c.get("run_log_record_id"),
                                    "runLogSubmittedAtMs": c.get("run_log_submitted_at_ms"),
                                    "split_group": c.get("split_group"),
                                    "split_total": c.get("split_total"),
                                    "split_index": c.get("split_index"),
                                    "append_output": c.get("append_output"),
                                }
                                if tk:
                                    tcfg = ctx.bitable_configs.get(tk) if hasattr(ctx, "bitable_configs") else None
                                    if tcfg:
                                        if getattr(tcfg, "app_token", None):
                                            cctx["appToken"] = tcfg.app_token
                                        if getattr(tcfg, "table_id", None):
                                            cctx["tableId"] = tcfg.table_id
                        if cctx:
                            payload["extra_data"] = {"callback_context": cctx}
                        return payload
                    elif provider in ("comfyui", ""):
                        from .comfyui import ComfyUIClient

                        base_url = str(info.get("comfyui_base_url") or "").strip() or str(getattr(getattr(ctx, "settings", None), "comfyui_base_url", "") or "")
                        if not base_url:
                            return None
                        cli = ComfyUIClient(base_url)
                        item = await cli.get_history_item(prompt_id=pid)
                        if not isinstance(item, dict):
                            return None
                        return {
                            "provider": "comfyui",
                            "prompt_id": pid,
                            "completed": True,
                            "status": "success",
                            "context": {"comfyui_base_url": base_url},
                            "result": item,
                        }
                except Exception:
                    logging.exception("poller: query failed for prompt_id=%s provider=%s", pid, provider)
                return None

            # 并发查询所有 pending 任务的状态
            results = await asyncio.gather(
                *[_query_one(pid, info) for pid, info in sorted_pending],
                return_exceptions=True,
            )

            # 第二步：并发处理已完成的回调（最多 5 个并行）
            # 所有任务在 3 方平台都已完成，download + upload + writeback 各自独立，并发执行
            _cb_sem = asyncio.Semaphore(10)

            async def _process_one(payload: dict[str, Any]) -> None:
                pid = payload.get("prompt_id")
                async with _cb_sem:
                    try:
                        from .callback_server import handle_callback_payload
                        await handle_callback_payload(ctx, payload)
                    except Exception:
                        logging.exception("poller: handle_callback_payload failed for prompt_id=%s", pid)
                        # 尽可能把表格状态改为"失败"，避免记录卡在"执行中"
                        try:
                            cctx = None
                            async with self._lock:
                                c = self._prompt_ctx.get(pid)
                                if isinstance(c, dict):
                                    cctx = dict(c)
                            if cctx:
                                tk = str(cctx.get("table_key") or "").strip()
                                rid = str(cctx.get("record_id") or "").strip()
                                if tk and rid:
                                    bitable = ctx.bitables.get(tk) if ctx.bitables else None
                                    tcfg = ctx.bitable_configs.get(tk) if ctx.bitable_configs else None
                                    if bitable and tcfg and getattr(bitable, "mode", None) and getattr(bitable.mode, "write_enabled", False):
                                        status_field = tcfg.fields.get("status")
                                        failed_value = tcfg.status_values.get("failed")
                                        if status_field and failed_value:
                                            await bitable.update_record(rid, {status_field: failed_value})
                                        error_field = tcfg.fields.get("error")
                                        if error_field:
                                            await bitable.update_record(rid, {error_field: "轮询处理回调时发生内部错误，请重新触发"})
                        except Exception:
                            logging.exception("poller: failed to write failure status for prompt_id=%s", pid)
                    async with self._lock:
                        self._pending_remote.pop(pid, None)

            await asyncio.gather(
                *[_process_one(p) for p in results if isinstance(p, dict)],
                return_exceptions=True,
            )

    def _run_key(self, table_key: str, workflow_key: str) -> str:
        return f"{table_key}::{workflow_key}"

    async def register_record_run(
        self,
        *,
        record_id: str,
        table_key: str,
        workflow_key: str,
        planned_total: int,
    ) -> None:
        rid = str(record_id or "").strip()
        tk = str(table_key or "").strip()
        wk = str(workflow_key or "").strip()
        if not rid or not tk or not wk:
            return
        rk = self._run_key(tk, wk)
        async with self._lock:
            key = (rk, rid)
            cur = self._record_runs.get(key)
            planned = max(1, int(planned_total) if isinstance(planned_total, int) else 1)
            if not cur:
                self._record_runs[key] = _RecordRunState(planned_total=planned, submitted_total=None, pending=set(), done=set())
            else:
                cur.planned_total = max(cur.planned_total, planned)

    async def finalize_record_run(
        self,
        *,
        record_id: str,
        table_key: str,
        workflow_key: str,
        submitted_total: int,
    ) -> None:
        rid = str(record_id or "").strip()
        tk = str(table_key or "").strip()
        wk = str(workflow_key or "").strip()
        if not rid or not tk or not wk:
            return
        rk = self._run_key(tk, wk)
        async with self._lock:
            key = (rk, rid)
            cur = self._record_runs.get(key)
            if not cur:
                self._record_runs[key] = _RecordRunState(
                    planned_total=max(1, int(submitted_total) if isinstance(submitted_total, int) else 1),
                    submitted_total=max(0, int(submitted_total) if isinstance(submitted_total, int) else 0),
                    pending=set(),
                    done=set(),
                )
            else:
                cur.submitted_total = max(0, int(submitted_total) if isinstance(submitted_total, int) else 0)

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
            i = max(1, min(int(inflight), 20))
            for k0, st0 in list(self._runs.items()):
                if not st0 or not st0.active:
                    continue
                if st0.table_key == table_key and st0.workflow_key != workflow_key:
                    st0.active = False
            rk = self._run_key(table_key, workflow_key)
            cur = self._runs.get(rk)
            if cur and cur.active and cur.inflight > 0:
                cur.batch = b
                cur.inflight_limit = i
                cur.drain = bool(drain)
                cur.chat_id = chat_id
                cur.remaining = None if bool(drain) else max(0, b)
                cur.empty_claims = 0
                cur.active = True
            else:
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
                    remaining=None if bool(drain) else max(0, b),
                    empty_claims=0,
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
        rk: str | None = None
        record_id: str | None = None
        should_fill = False
        async with self._lock:
            temp_files = self._prompt_temp_files.pop(prompt_id, [])
            ctx0 = self._prompt_ctx.pop(prompt_id, None)
            self._pending_remote.pop(prompt_id, None)
            rk = self._prompt_to_run_key.pop(prompt_id, None)
            st = self._runs.get(rk) if rk else None

            if not rk and isinstance(ctx0, dict):
                tk = str(ctx0.get("table_key") or "").strip()
                wk = str(ctx0.get("workflow_key") or "").strip()
                if tk and wk:
                    rk = self._run_key(tk, wk)
                    st = self._runs.get(rk)
            if isinstance(ctx0, dict):
                rid = ctx0.get("record_id")
                record_id = str(rid).strip() if isinstance(rid, str) and str(rid).strip() else None

            if st and rk and record_id:
                key = (rk, record_id)
                rs = self._record_runs.get(key)
                if not rs:
                    rs = _RecordRunState(planned_total=1, submitted_total=None, pending=set(), done=set())
                    self._record_runs[key] = rs
                rs.pending.discard(prompt_id)
                rs.done.add(prompt_id)
                expected = rs.submitted_total if rs.submitted_total is not None else rs.planned_total
                expected = max(0, int(expected) if isinstance(expected, int) else 0)
                if expected <= 0:
                    expected = len(rs.pending) + len(rs.done)
                if len(rs.done) >= expected and len(rs.pending) <= 0:
                    self._record_runs.pop(key, None)
                    st.inflight = max(0, st.inflight - 1)
                    should_fill = True
            elif st and rk and not record_id:
                st.inflight = max(0, st.inflight - 1)
                should_fill = True

            if st and rk and record_id:
                st.prompt_to_record.pop(prompt_id, None)

        if temp_files and self._ctx:
            base_dir = str(getattr(getattr(self._ctx, "settings", None), "temp_download_dir", "") or "").strip()
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

        if should_fill and rk:
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
        user_open_id: str | None = None,
        run_log_table_key: str | None = None,
        run_log_record_id: str | None = None,
        run_log_submitted_at_ms: int | None = None,
        split_group: str | None = None,
        split_total: int | None = None,
        split_index: int | None = None,
        append_output: bool | None = None,
    ) -> None:
        pid = str(prompt_id or "").strip()
        if not pid:
            return
        rid = str(record_id or "").strip()
        tk = str(table_key or "").strip()
        wk = str(workflow_key or "").strip()
        async with self._lock:
            self._prompt_ctx[pid] = {
                "record_id": record_id,
                "table_key": table_key,
                "workflow_key": workflow_key,
                "chat_id": chat_id,
                "user_open_id": user_open_id,
                "run_log_table_key": run_log_table_key,
                "run_log_record_id": run_log_record_id,
                "run_log_submitted_at_ms": run_log_submitted_at_ms,
                "split_group": split_group,
                "split_total": split_total,
                "split_index": split_index,
                "append_output": append_output,
            }
            if rid and tk and wk:
                rk = self._run_key(tk, wk)
                key = (rk, rid)
                rs = self._record_runs.get(key)
                if not rs:
                    rs = _RecordRunState(planned_total=1, submitted_total=None, pending=set(), done=set())
                    self._record_runs[key] = rs
                rs.pending.add(pid)

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

    async def register_ignored_prompt_ids(self, *, prompt_ids: list[str]) -> None:
        now = time.time()
        clean_ids = [str(x or "").strip() for x in (prompt_ids or []) if str(x or "").strip()]
        if not clean_ids:
            return
        async with self._lock:
            for pid0, ts0 in list(self._ignored_prompt_ids.items()):
                if not pid0 or (now - float(ts0 or 0.0)) > 3600:
                    self._ignored_prompt_ids.pop(pid0, None)
            for pid in clean_ids:
                self._ignored_prompt_ids[pid] = now

    async def consume_ignored_prompt(self, *, prompt_id: str) -> bool:
        pid = str(prompt_id or "").strip()
        if not pid:
            return False
        now = time.time()
        async with self._lock:
            for pid0, ts0 in list(self._ignored_prompt_ids.items()):
                if not pid0 or (now - float(ts0 or 0.0)) > 3600:
                    self._ignored_prompt_ids.pop(pid0, None)
            hit = pid in self._ignored_prompt_ids
            if hit:
                self._ignored_prompt_ids.pop(pid, None)
            return hit

    async def resolve_prompt(self, *, prompt_id: str) -> dict[str, Any] | None:
        async with self._lock:
            c = self._prompt_ctx.get(prompt_id)
            if isinstance(c, dict):
                return {
                    "record_id": c.get("record_id"),
                    "table_key": c.get("table_key"),
                    "workflow_key": c.get("workflow_key"),
                    "chat_id": c.get("chat_id"),
                    "user_open_id": c.get("user_open_id"),
                    "run_log_table_key": c.get("run_log_table_key"),
                    "run_log_record_id": c.get("run_log_record_id"),
                    "run_log_submitted_at_ms": c.get("run_log_submitted_at_ms"),
                    "split_group": c.get("split_group"),
                    "split_total": c.get("split_total"),
                    "split_index": c.get("split_index"),
                    "append_output": c.get("append_output"),
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
                "user_open_id": None,
            }

    async def _fill(self, rk: str) -> None:
        async with self._lock:
            if rk in self._filling:
                return
            self._filling.add(rk)

        try:
            while True:
                async with self._lock:
                    st = self._runs.get(rk)
                    if not st or not st.active:
                        return
                    if not st.drain and isinstance(st.remaining, int) and st.remaining <= 0:
                        if st.inflight <= 0:
                            st.active = False
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

                    if not st.drain and isinstance(st.remaining, int):
                        needed = min(needed, max(0, st.remaining))
                        if needed <= 0:
                            if st.inflight <= 0:
                                st.active = False
                            return

                ids = await self._claim_records(
                    rk=rk,
                    limit=min(needed, st.batch) if st.drain else needed,
                    status_field=status_field,
                    queued_value=queued_value,
                    running_value=running_value,
                    sort_field=created_time_field,
                )
                async with self._lock:
                    st3 = self._runs.get(rk)
                    if st3 and ids:
                        st3.empty_claims = 0
                        st3.total_claimed = int(getattr(st3, "total_claimed", 0) or 0) + len(ids)
                    if st3 and not st3.drain and isinstance(st3.remaining, int):
                        st3.remaining = max(0, int(st3.remaining) - len(ids))
                if not ids:
                    async with self._lock:
                        st2 = self._runs.get(rk)
                        if st2 and st2.active and st2.inflight == 0:
                            if st2.drain:
                                # 飞书搜索偶尔会有短暂延迟，drain 模式下先重试几次，别因为一次空结果就提前停掉。
                                st2.empty_claims = int(getattr(st2, "empty_claims", 0) or 0) + 1
                                should_retry = st2.empty_claims < 3
                            else:
                                should_retry = False
                            
                            if not should_retry:
                                st2.active = False
                                # 如果是第一次进来就发现没数据（从没成功抢过单），才发送友好提示
                                # 如果已经成功抢过单了（表示已经处理完了一批），就不再打扰用户
                                if st2.chat_id and getattr(st2, "total_claimed", 0) == 0:
                                    ctx0 = self._ctx
                                    if ctx0:
                                        try:
                                            from .im import IMClient
                                            im = IMClient(ctx0.auth)
                                            msg = f"队列 {st2.workflow_key} 在表 {st2.table_key} 中未找到可抢的“待处理”记录。可能是被过滤掉或字段缺失。"
                                            asyncio.create_task(im.send_text(chat_id=st2.chat_id, text=msg))
                                        except Exception as e:
                                            logging.error("Failed to send empty claim notice: %s", e)
                        else:
                            should_retry = False
                    if should_retry:
                        await asyncio.sleep(1.5)
                        continue
                    async with self._lock:
                        st4 = self._runs.get(rk)
                        if st4 and st4.active and st4.inflight == 0:
                            st4.active = False
                    return

                for record_id in ids:
                    await self._queue_one(rk=rk, record_id=record_id)
        finally:
            async with self._lock:
                self._filling.discard(rk)

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
        return await _enc_claim_records(
            bitable,
            limit=limit,
            status_field=status_field,
            queued_value=queued_value,
            running_value=running_value,
            sort_field=sort_field,
        )

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
            if prompt_id:
                logging.info("_queue_one success: workflow=%s record_id=%s prompt_id=%s", st.workflow_key, record_id, prompt_id)
            else:
                logging.error("_queue_one failed (no prompt_id returned): workflow=%s record_id=%s", st.workflow_key, record_id)
        except Exception as e:
            err = str(e)
            logging.exception("_queue_one run_workflow exception: workflow=%s record_id=%s err=%s", st.workflow_key, record_id, err)

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
                    rs = self._record_runs.get((rk, record_id))
                    has_pending = bool(rs and (rs.pending or (rs.submitted_total is not None and rs.submitted_total > 0)))
                    if not has_pending:
                        st2.inflight = max(0, st2.inflight - 1)

        if prompt_id and bitable and table_cfg and bitable.mode.write_enabled:
            allow_write_prompt_id = True
            try:
                wf_cfg = (ctx.config.get("workflows") or {}).get(st.workflow_key) or {}
                wf_fields = wf_cfg.get("writeBackFields")
                if isinstance(wf_fields, dict):
                    allow_write_prompt_id = "prompt_id" in wf_fields
            except Exception:
                allow_write_prompt_id = True

            prompt_field = table_cfg.fields.get("prompt_id")
            if allow_write_prompt_id and prompt_field:
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
            allow_write_error = True
            try:
                wf_cfg = (ctx.config.get("workflows") or {}).get(st.workflow_key) or {}
                wf_fields = wf_cfg.get("writeBackFields")
                if isinstance(wf_fields, dict):
                    allow_write_error = "error" in wf_fields
            except Exception:
                allow_write_error = True
            if allow_write_error and error_field:
                fields[error_field] = err or "queue failed"
            if fields:
                try:
                    await bitable.update_record(record_id, fields)
                except Exception:
                    pass
