import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import biaoge_bot.main as main_mod
from biaoge_bot.main import _parse_bitable_mode, build_context
from biaoge_bot.ports import (
    BitableConfig,
    BitableMode,
    ctx_bitable_event_enabled,
    ctx_bitable_input_read_enabled,
)


def _write_case_files(tmp: Path, *, mode: str, with_tables: bool) -> Path:
    cfg_path = tmp / "workflows.json"
    if with_tables:
        cfg_path.write_text(
            """
{
  "default_table": "demo",
  "tables": {
    "demo": {
      "app_token": "app_token_demo",
      "table_id": "table_demo",
      "fields": {"status": "任务状态", "prompt_id": "prompt_id"},
      "status_values": {"queued": "待处理", "running": "执行中"}
    }
  },
  "workflows": {
    "default": {"workflowName": "Default", "params": {}}
  }
}
""".strip()
            + "\n",
            encoding="utf-8",
        )
    else:
        cfg_path.write_text('{"workflows": {"default": {"workflowName": "Default", "params": {}}}}\n', encoding="utf-8")

    env_path = tmp / f"{mode}.env"
    env_path.write_text(
        "\n".join(
            [
                "FEISHU_APP_ID=test_app_id",
                "FEISHU_APP_SECRET=test_app_secret",
                f"BITABLE_MODE={mode}",
                f"WORKFLOW_CONFIG_PATH={cfg_path}",
                "BOT_LOG_LEVEL=INFO",
                "CALLBACK_HOST=127.0.0.1",
                "CALLBACK_PORT=9901",
                "COMFYUI_BASE_URL=http://127.0.0.1:8188",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return env_path


def test_parse_modes() -> None:
    expected = {
        "off": (False, False),
        "read": (True, False),
        "write": (True, True),
        "readwrite": (True, True),
        "auto": (True, True),
        "unknown": (True, True),
    }
    for mode, pair in expected.items():
        parsed = _parse_bitable_mode(mode)
        assert (parsed.read_enabled, parsed.write_enabled) == pair, mode


def test_context_capabilities() -> None:
    old_check_license = main_mod.check_license
    main_mod.check_license = lambda: SimpleNamespace(ok=True, device_code="", license_path="")
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            for mode in ("off", "read", "write", "readwrite", "auto"):
                env_path = _write_case_files(tmp, mode=mode, with_tables=True)
                ctx = build_context(str(env_path))
                if mode == "off":
                    assert not ctx.bitables
                    assert ctx.drive is None
                    assert not ctx_bitable_input_read_enabled(ctx)
                    assert not ctx_bitable_event_enabled(ctx)
                    continue

                assert ctx.bitables, mode
                assert ctx.drive is not None, mode
                if mode == "write":
                    assert not ctx_bitable_input_read_enabled(ctx)
                    assert not ctx_bitable_event_enabled(ctx)
                else:
                    assert ctx_bitable_input_read_enabled(ctx)
                    assert ctx_bitable_event_enabled(ctx)

            env_path = _write_case_files(tmp, mode="auto", with_tables=False)
            ctx = build_context(str(env_path))
            assert not ctx.bitables
            assert ctx.drive is None
            assert not ctx_bitable_input_read_enabled(ctx)
            assert not ctx_bitable_event_enabled(ctx)
    finally:
        main_mod.check_license = old_check_license


def test_event_subscription_gate() -> None:
    calls: list[list[str]] = []

    async def fake_subscribe_bitable_files(*, auth, file_tokens):
        calls.append(list(file_tokens))
        return [{"ok": True, "file_token": token} for token in file_tokens]

    old_subscribe = main_mod.subscribe_bitable_files
    main_mod.subscribe_bitable_files = fake_subscribe_bitable_files
    try:
        cfg = BitableConfig(
            app_token="app_token_demo",
            table_id="table_demo",
            view_id=None,
            fields={},
            status_values={},
        )
        for mode, parsed, should_call in (
            ("off", BitableMode(False, False), False),
            ("write", BitableMode(True, True), False),
            ("read", BitableMode(True, False), True),
            ("readwrite", BitableMode(True, True), True),
            ("auto", BitableMode(True, True), True),
        ):
            calls.clear()
            ctx = SimpleNamespace(
                settings=SimpleNamespace(bitable_mode=mode),
                bitable_mode=parsed,
                bitable_configs={"demo": cfg},
                auth=SimpleNamespace(),
            )
            main_mod._warm_bitable_event_subscriptions(ctx)
            assert bool(calls) is should_call, mode
    finally:
        main_mod.subscribe_bitable_files = old_subscribe


def run_all() -> None:
    test_parse_modes()
    test_context_capabilities()
    test_event_subscription_gate()
    print("Bitable mode semantic tests passed!")


if __name__ == "__main__":
    run_all()
