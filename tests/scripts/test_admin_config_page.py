import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi import HTTPException

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from biaoge_bot.admin_config import (
    _admin_page_html,
    _normalize_env_value,
    _visible_env_schema,
    _visible_env_values,
    register_admin,
)
from biaoge_bot.callback_server import _filename_from_url
from biaoge_bot.main import build_context


def test_admin_static_assets() -> None:
    html = _admin_page_html()
    assert "/admin/static/admin.css?v=" in html
    assert "/admin/static/admin.js?v=" in html
    assert "{{ASSET_VERSION}}" not in html

    static_dir = PROJECT_ROOT / "biaoge_bot" / "admin_static"
    assert (static_dir / "admin.html").exists()
    assert (static_dir / "admin.css").exists()
    assert (static_dir / "admin.js").exists()

    js_check = subprocess.run(
        ["node", "--check", str(static_dir / "admin.js")],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
    )
    assert js_check.returncode == 0, js_check.stderr or js_check.stdout


def test_admin_routes_and_schema() -> None:
    os.environ["ADMIN_TOKEN"] = "admin-config-test-token"
    app = FastAPI()
    register_admin(app, SimpleNamespace())
    client = TestClient(app)

    page = client.get("/admin/config?token=admin-config-test-token")
    assert page.status_code == 200, page.text
    assert "/admin/static/admin.css?v=" in page.text
    assert "/admin/static/admin.js?v=" in page.text

    css = client.get("/admin/static/admin.css")
    js = client.get("/admin/static/admin.js")
    assert css.status_code == 200, css.text
    assert js.status_code == 200, js.text
    assert "text/css" in css.headers.get("content-type", "")
    assert "javascript" in js.headers.get("content-type", "")

    visible = _visible_env_values({})
    schema = _visible_env_schema()
    assert "ADMIN_TOKEN" not in visible
    assert "FEISHU_APP_SECRET" not in visible
    assert "RUNNINGHUB_API_KEY" not in visible
    assert visible["BOT_LOG_LEVEL"] == "INFO"
    assert visible["CALLBACK_DUMP_ENABLED"] == "0"
    assert visible["SAVE_TASK_REQUEST_PARAMS"] == "0"
    assert schema["BOT_LOG_LEVEL"]["type"] == "select"
    assert schema["CALLBACK_DUMP_ENABLED"]["type"] == "switch"
    assert schema["SAVE_TASK_REQUEST_PARAMS"]["type"] == "switch"
    assert "FEISHU_SEND_RESULT_TO_CHAT" in _visible_env_values({"BITABLE_MODE": "readwrite"})
    assert "FEISHU_SEND_RESULT_TO_CHAT" in _visible_env_schema({"BITABLE_MODE": "readwrite"})
    assert "FEISHU_SEND_RESULT_TO_CHAT" not in _visible_env_values({"BITABLE_MODE": "off"})
    assert "FEISHU_SEND_RESULT_TO_CHAT" not in _visible_env_schema({"BITABLE_MODE": "off"})


def test_env_normalization() -> None:
    assert _normalize_env_value("CALLBACK_DUMP_ENABLED", "yes") == "1"
    assert _normalize_env_value("CALLBACK_DUMP_ENABLED", "") == "0"
    assert _normalize_env_value("SAVE_TASK_REQUEST_PARAMS", True) == "1"
    assert _normalize_env_value("SAVE_TASK_REQUEST_PARAMS", False) == "0"
    assert _normalize_env_value("BOT_LOG_LEVEL", "debug") == "DEBUG"

    try:
        _normalize_env_value("BOT_LOG_LEVEL", "verbose")
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("invalid BOT_LOG_LEVEL should fail")


def test_comfyui_view_url_filename() -> None:
    url = "http://127.0.0.1:8188/view?filename=ComfyUI_00028_.png&type=output"
    assert _filename_from_url(url) == "ComfyUI_00028_.png"


def test_compile_and_context_entrypoint() -> None:
    import py_compile

    cfile = Path(tempfile.gettempdir()) / "biaoge_admin_config_test.pyc"
    py_compile.compile(str(PROJECT_ROOT / "biaoge_bot" / "admin_config.py"), cfile=str(cfile), doraise=True)

    ctx = build_context()
    assert type(ctx).__name__ == "AppContext"
    assert ctx.settings.callback_port > 0


def run_all() -> None:
    test_admin_static_assets()
    test_admin_routes_and_schema()
    test_env_normalization()
    test_comfyui_view_url_filename()
    test_compile_and_context_entrypoint()
    print("Admin config page tests passed!")


if __name__ == "__main__":
    run_all()
