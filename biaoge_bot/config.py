from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            return os.environ.get(m.group(1), "")

        return _ENV_VAR_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_json_with_env(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    return _expand_env(data)


@dataclass(frozen=True)
class Settings:
    feishu_app_id: str
    feishu_app_secret: str
    bitable_app_token: str
    bitable_table_id: str
    bitable_mode: str
    comfyui_input_dir: str | None
    temp_download_dir: str
    result_output_dir: str
    comfyui_base_url: str
    comfyui_upload_enabled: bool
    comfyui_upload_subfolder: str | None
    comfyui_upload_overwrite: bool
    remote_callback_url: str | None
    cb_message_token: str | None
    runninghub_api_key: str | None
    remote_result_mode: str
    remote_poll_interval_seconds: int
    remote_poll_fallback_seconds: int
    pending_timeout_seconds: int
    callback_host: str
    callback_port: int
    callback_path: str
    workflow_config_path: str | None
    bot_log_level: str

    @property
    def callback_url(self) -> str:
        return f"http://{self.callback_host}:{self.callback_port}{self.callback_path}"


def load_settings(env_path: str | None = None) -> Settings:
    load_dotenv(env_path, override=True)

    def need(key: str) -> str:
        v = os.environ.get(key, "").strip()
        if not v:
            raise RuntimeError(f"missing env: {key}")
        return v

    def to_bool(value: str) -> bool:
        v = (value or "").strip().lower()
        return v in ("1", "true", "yes", "y", "on")

    def to_int(value: str, default: int) -> int:
        s = str(value or "").strip()
        if not s:
            return int(default)
        try:
            return int(float(s))
        except Exception:
            return int(default)

    return Settings(
        feishu_app_id=need("FEISHU_APP_ID"),
        feishu_app_secret=need("FEISHU_APP_SECRET"),
        bitable_app_token=os.environ.get("BITABLE_APP_TOKEN", "").strip(),
        bitable_table_id=os.environ.get("BITABLE_TABLE_ID", "").strip(),
        bitable_mode=os.environ.get("BITABLE_MODE", "auto").strip().lower(),
        comfyui_input_dir=(os.environ.get("COMFYUI_INPUT_DIR", "").strip() or None),
        temp_download_dir=os.path.join(os.getcwd(), "temp_downloads"),
        result_output_dir=os.environ.get("RESULT_OUTPUT_DIR", "").strip(),
        comfyui_base_url=os.environ.get("COMFYUI_BASE_URL", "http://127.0.0.1:8188").strip(),
        comfyui_upload_enabled=to_bool(os.environ.get("COMFYUI_UPLOAD_ENABLED", "0")),
        comfyui_upload_subfolder=(os.environ.get("COMFYUI_UPLOAD_SUBFOLDER", "").strip() or None),
        comfyui_upload_overwrite=to_bool(os.environ.get("COMFYUI_UPLOAD_OVERWRITE", "true")),
        remote_callback_url=(os.environ.get("REMOTE_CALLBACK_URL", "").strip() or None),
        cb_message_token=(os.environ.get("CB_MESSAGE_TOKEN", "").strip() or None),
        runninghub_api_key=(os.environ.get("RUNNINGHUB_API_KEY", "").strip() or None),
        remote_result_mode=os.environ.get("REMOTE_RESULT_MODE", "poll").strip().lower() or "poll",
        remote_poll_interval_seconds=max(10, to_int(os.environ.get("REMOTE_POLL_INTERVAL_SECONDS", "60"), 60)),
        remote_poll_fallback_seconds=max(0, to_int(os.environ.get("REMOTE_POLL_FALLBACK_SECONDS", "600"), 600)),
        pending_timeout_seconds=max(0, to_int(os.environ.get("PENDING_TIMEOUT_SECONDS", "7200"), 7200)),
        callback_host=os.environ.get("CALLBACK_HOST", "127.0.0.1").strip(),
        callback_port=int(os.environ.get("CALLBACK_PORT", "9901").strip()),
        callback_path=os.environ.get("CALLBACK_PATH", "/comfyui/callback").strip(),
        workflow_config_path=(os.environ.get("WORKFLOW_CONFIG_PATH", "").strip() or None),
        bot_log_level=os.environ.get("BOT_LOG_LEVEL", "INFO").strip(),
    )
