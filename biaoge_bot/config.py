from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _expand_env_string(value: str) -> str:
    def repl(m: re.Match[str]) -> str:
        return os.environ.get(m.group(1), "")

    return _ENV_VAR_PATTERN.sub(repl, value)


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _expand_env_string(value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_json_with_env(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    return _expand_env(data)


def resolve_project_path(value: str | Path, *, root: str | Path | None = None) -> str:
    raw = str(value or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    base = Path(root) if root is not None else project_root()
    os.environ.setdefault("BIAOGE_ROOT", str(base))
    expanded = os.path.expandvars(_expand_env_string(raw))
    p = Path(expanded).expanduser()
    if not p.is_absolute():
        p = base / p
    return str(p.resolve())


def resolve_optional_path(value: str | Path | None, *, root: str | Path | None = None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    return resolve_project_path(raw, root=root)


def normalize_config_paths(config: dict[str, Any], *, root: str | Path | None = None) -> dict[str, Any]:
    if not isinstance(config, dict):
        return config
    workflows = config.get("workflows")
    if not isinstance(workflows, dict):
        return config
    for raw in workflows.values():
        if not isinstance(raw, dict):
            continue
        for key in ("apiWorkflowPath", "api_workflow_path"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                raw[key] = resolve_project_path(value, root=root)
    return config


@dataclass(frozen=True)
class Settings:
    project_root: str
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
    comfyui_upload_timeout_seconds: int
    remote_callback_url: str | None
    cb_message_token: str | None
    runninghub_api_key: str | None
    feishu_send_result_to_chat: bool
    remote_result_mode: str
    remote_poll_interval_seconds: int
    remote_poll_fallback_seconds: int
    pending_timeout_seconds: int
    callback_host: str
    callback_port: int
    callback_path: str
    workflow_config_path: str | None
    callback_dump_enabled: bool
    callback_dump_dir: str | None
    bot_log_level: str
    save_task_request_params: bool
    task_request_dump_dir: str | None

    @property
    def callback_url(self) -> str:
        return f"http://{self.callback_host}:{self.callback_port}{self.callback_path}"


def load_settings(env_path: str | None = None) -> Settings:
    root = project_root()
    load_dotenv(env_path or (root / ".env"), override=True)
    os.environ.setdefault("BIAOGE_ROOT", str(root))
    license_path = resolve_optional_path(os.environ.get("BIAOGE_LICENSE_PATH", ""), root=root)
    if license_path:
        os.environ["BIAOGE_LICENSE_PATH"] = license_path

    def need(key: str) -> str:
        v = os.environ.get(key, "").strip()
        if not v:
            raise RuntimeError(f"missing env: {key}")
        return v

    def to_int(value: str, default: int) -> int:
        s = str(value or "").strip()
        if not s:
            return int(default)
        try:
            return int(float(s))
        except Exception:
            return int(default)

    def to_bool(value: str) -> bool:
        v = (value or "").strip().lower()
        return v in ("1", "true", "yes", "y", "on")

    callback_dump_enabled = to_bool(os.environ.get("CALLBACK_DUMP_ENABLED", "0"))
    save_task_request_params = to_bool(os.environ.get("SAVE_TASK_REQUEST_PARAMS", "0"))
    callback_dump_dir = str((root / "logs" / "dumps" / "callbacks").resolve()) if callback_dump_enabled else None
    task_request_dump_dir = str((root / "logs" / "dumps" / "task_requests").resolve()) if save_task_request_params else None

    return Settings(
        project_root=str(root),
        feishu_app_id=need("FEISHU_APP_ID"),
        feishu_app_secret=need("FEISHU_APP_SECRET"),
        bitable_app_token=os.environ.get("BITABLE_APP_TOKEN", "").strip(),
        bitable_table_id=os.environ.get("BITABLE_TABLE_ID", "").strip(),
        bitable_mode=os.environ.get("BITABLE_MODE", "auto").strip().lower(),
        comfyui_input_dir=resolve_optional_path(os.environ.get("COMFYUI_INPUT_DIR", ""), root=root),
        temp_download_dir=resolve_project_path(os.environ.get("TEMP_DOWNLOAD_DIR", "temp_downloads"), root=root),
        result_output_dir=resolve_optional_path(os.environ.get("RESULT_OUTPUT_DIR", ""), root=root) or "",
        comfyui_base_url=os.environ.get("COMFYUI_BASE_URL", "http://127.0.0.1:8188").strip(),
        comfyui_upload_enabled=to_bool(os.environ.get("COMFYUI_UPLOAD_ENABLED", "0")),
        comfyui_upload_subfolder=(os.environ.get("COMFYUI_UPLOAD_SUBFOLDER", "").strip() or None),
        comfyui_upload_overwrite=to_bool(os.environ.get("COMFYUI_UPLOAD_OVERWRITE", "true")),
        comfyui_upload_timeout_seconds=max(3, to_int(os.environ.get("COMFYUI_UPLOAD_TIMEOUT_SECONDS", "20"), 20)),
        remote_callback_url=(os.environ.get("REMOTE_CALLBACK_URL", "").strip() or None),
        cb_message_token=(os.environ.get("CB_MESSAGE_TOKEN", "").strip() or None),
        runninghub_api_key=(os.environ.get("RUNNINGHUB_API_KEY", "").strip() or None),
        feishu_send_result_to_chat=to_bool(os.environ.get("FEISHU_SEND_RESULT_TO_CHAT", "0")),
        remote_result_mode=os.environ.get("REMOTE_RESULT_MODE", "poll").strip().lower() or "poll",
        remote_poll_interval_seconds=max(10, to_int(os.environ.get("REMOTE_POLL_INTERVAL_SECONDS", "60"), 60)),
        remote_poll_fallback_seconds=max(0, to_int(os.environ.get("REMOTE_POLL_FALLBACK_SECONDS", "600"), 600)),
        pending_timeout_seconds=max(0, to_int(os.environ.get("PENDING_TIMEOUT_SECONDS", "7200"), 7200)),
        callback_host=os.environ.get("CALLBACK_HOST", "127.0.0.1").strip(),
        callback_port=int(os.environ.get("CALLBACK_PORT", "9901").strip()),
        callback_path=os.environ.get("CALLBACK_PATH", "/comfyui/callback").strip(),
        workflow_config_path=resolve_optional_path(os.environ.get("WORKFLOW_CONFIG_PATH", ""), root=root),
        callback_dump_enabled=callback_dump_enabled,
        callback_dump_dir=callback_dump_dir,
        bot_log_level=os.environ.get("BOT_LOG_LEVEL", "INFO").strip(),
        save_task_request_params=save_task_request_params,
        task_request_dump_dir=task_request_dump_dir,
    )
