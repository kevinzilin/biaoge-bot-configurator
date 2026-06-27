from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import shutil
import socket
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
WORKFLOWS_LOCAL = ROOT / "config" / "workflows.local.json"
WORKFLOWS_EXAMPLE = ROOT / "config" / "workflows.example.json"

WORKFLOWS_LOCAL_SKELETON = """\
{
  "_comment": "Minimal config skeleton. See config/workflows.example.json for full reference.",
  "default_table": "",
  "default_workflow": "",
  "tables": {},
  "automation": {},
  "workflows": {}
}
"""

DEFAULT_PATHS = {
    "WORKFLOW_CONFIG_PATH": "config/workflows.local.json",
    "RESULT_OUTPUT_DIR": "output",
}

REQUIRED_ENV_KEYS = [
    ("FEISHU_APP_ID", "Feishu App ID"),
    ("FEISHU_APP_SECRET", "Feishu App Secret"),
]


def venv_python() -> Path:
    if os.name == "nt":
        return ROOT / ".venv" / "Scripts" / "python.exe"
    return ROOT / ".venv" / "bin" / "python"


def ensure_venv() -> None:
    py = venv_python()
    if not py.exists():
        install_cmd = "install.cmd" if os.name == "nt" else "./install.sh"
        raise SystemExit(f"Virtual env [.venv] not found. Please run {install_cmd} first.")


def ensure_env_file() -> None:
    if ENV_PATH.exists():
        return
    if (ROOT / ".env.example").exists():
        shutil.copy2(ROOT / ".env.example", ENV_PATH)
        print(".env created from .env.example.")
    else:
        ENV_PATH.write_text("", encoding="utf-8")
        print(".env created [empty].")


def read_dotenv(path: Path = ENV_PATH) -> dict[str, str]:
    env_map: dict[str, str] = {}
    if not path.exists():
        return env_map
    for raw_line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        if key:
            env_map[key] = value.strip()
    return env_map


def update_dotenv(updates: dict[str, str], path: Path = ENV_PATH) -> None:
    lines = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines() if path.exists() else []
    existing: set[str] = set()
    out: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip().lstrip("\ufeff")
            if key in updates:
                out.append(f"{key}={updates[key]}")
                existing.add(key)
                continue
            if key:
                existing.add(key)
        out.append(line)

    for key, value in updates.items():
        if key.strip() and key not in existing:
            out.append(f"{key}={value}")

    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def resolve_project_path(value: str | None) -> Path | None:
    raw = (value or "").strip().strip('"').strip("'")
    if not raw:
        return None
    os.environ.setdefault("BIAOGE_ROOT", str(ROOT))
    expanded = os.path.expanduser(os.path.expandvars(raw.replace("${BIAOGE_ROOT}", str(ROOT))))
    path = Path(expanded)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def display_path(path: Path) -> str:
    try:
        rel = path.resolve().relative_to(ROOT.resolve())
        return rel.as_posix()
    except ValueError:
        return path.as_posix()


def ensure_workflows_local_config() -> None:
    if WORKFLOWS_LOCAL.exists():
        return
    WORKFLOWS_LOCAL.parent.mkdir(parents=True, exist_ok=True)
    if WORKFLOWS_EXAMPLE.exists():
        shutil.copy2(WORKFLOWS_EXAMPLE, WORKFLOWS_LOCAL)
        print("Created config/workflows.local.json from example.")
    else:
        WORKFLOWS_LOCAL.write_text(WORKFLOWS_LOCAL_SKELETON, encoding="utf-8")
        print("Created config/workflows.local.json (minimal skeleton).")


def normalize_env_paths(env_map: dict[str, str]) -> dict[str, str]:
    updates: dict[str, str] = {}
    for key, default_value in DEFAULT_PATHS.items():
        raw = env_map.get(key, "").strip()
        resolved = resolve_project_path(raw or default_value)
        if resolved is None:
            continue
        desired = display_path(resolved)
        if raw != desired:
            updates[key] = desired
            env_map[key] = desired

    for key in ("COMFYUI_INPUT_DIR", "BIAOGE_LICENSE_PATH"):
        raw = env_map.get(key, "").strip()
        if not raw:
            continue
        resolved = resolve_project_path(raw)
        if resolved is None:
            continue
        desired = display_path(resolved)
        if raw != desired:
            updates[key] = desired
            env_map[key] = desired

    if updates:
        update_dotenv(updates)
        for key, value in updates.items():
            print(f"{key} -> {value}")
    return updates


def repair_workflow_config_path(env_map: dict[str, str]) -> None:
    raw = env_map.get("WORKFLOW_CONFIG_PATH", "").strip()
    resolved = resolve_project_path(raw or "config/workflows.local.json")
    if resolved and resolved.exists():
        if resolved == WORKFLOWS_EXAMPLE.resolve():
            ensure_workflows_local_config()
            update_dotenv({"WORKFLOW_CONFIG_PATH": "config/workflows.local.json"})
            env_map["WORKFLOW_CONFIG_PATH"] = "config/workflows.local.json"
            print("WORKFLOW_CONFIG_PATH -> config/workflows.local.json")
        return

    ensure_workflows_local_config()
    update_dotenv({"WORKFLOW_CONFIG_PATH": "config/workflows.local.json"})
    env_map["WORKFLOW_CONFIG_PATH"] = "config/workflows.local.json"
    print("WORKFLOW_CONFIG_PATH fixed -> config/workflows.local.json")


def normalize_workflow_api_paths(env_map: dict[str, str]) -> None:
    workflow_config = resolve_project_path(env_map.get("WORKFLOW_CONFIG_PATH", "config/workflows.local.json"))
    if not workflow_config or not workflow_config.exists():
        return
    try:
        data = json.loads(workflow_config.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Warning: cannot parse workflow config {workflow_config}: {exc}")
        return

    changed = False
    workflows = data.get("workflows")
    if isinstance(workflows, dict):
        for item in workflows.values():
            if not isinstance(item, dict):
                continue
            raw = str(item.get("apiWorkflowPath", "") or "").strip()
            if not raw:
                continue
            resolved = resolve_project_path(raw)
            if resolved is None:
                continue
            desired = display_path(resolved)
            if raw != desired:
                item["apiWorkflowPath"] = desired
                changed = True

    if changed:
        workflow_config.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Normalized workflow paths in {display_path(workflow_config)}")


def parse_port(env_map: dict[str, str]) -> tuple[str, int]:
    host = env_map.get("CALLBACK_HOST", "127.0.0.1").strip().strip('"') or "127.0.0.1"
    raw_port = env_map.get("CALLBACK_PORT", "9901").strip().strip('"') or "9901"
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise SystemExit(f"Invalid CALLBACK_PORT: {raw_port}") from exc
    if not (1 <= port <= 65535):
        raise SystemExit(f"Invalid CALLBACK_PORT: {port}")
    return host, port


def check_port_available(host: str, port: int) -> None:
    bind_host = "127.0.0.1"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((bind_host, port))
    except OSError as exc:
        raise SystemExit(
            f"Port already in use: {host}:{port}\n"
            "Please close the existing process, or change CALLBACK_PORT in .env, then retry."
        ) from exc
    finally:
        sock.close()


def _collect_socks_proxy_sources(env_map: dict[str, str]) -> list[tuple[str, str]]:
    proxy_keys = (
        "ALL_PROXY",
        "all_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
    )
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for source in (os.environ, env_map):
        for key in proxy_keys:
            value = str(source.get(key, "") or "").strip()
            if not value:
                continue
            item = (key, value)
            if item in seen:
                continue
            seen.add(item)
            if value.lower().startswith(("socks://", "socks4://", "socks4a://", "socks5://", "socks5h://")):
                out.append(item)
    return out


def check_socks_proxy_dependency(env_map: dict[str, str]) -> None:
    sources = _collect_socks_proxy_sources(env_map)
    if not sources:
        return
    if importlib.util.find_spec("python_socks") is not None:
        return
    key, _value = sources[0]
    install_cmd = "install.cmd" if os.name == "nt" else "bash install.sh"
    pip_cmd = ".venv\\Scripts\\python.exe -m pip install \"python-socks[asyncio]>=2.4.4\"" if os.name == "nt" else ".venv/bin/python -m pip install 'python-socks[asyncio]>=2.4.4'"
    raise SystemExit(
        f"Detected SOCKS proxy in {key}, but the virtual env is missing python-socks.\n"
        f"Please run {install_cmd} to refresh dependencies, or run:\n{pip_cmd}"
    )

def prompt_value(key: str, description: str) -> str | None:
    print("")
    print(f"[{description}]")
    try:
        return input(f"{key} = ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return None


def ensure_required_config(env_map: dict[str, str], *, interactive: bool) -> dict[str, str]:
    updates: dict[str, str] = {}
    missing_required = [
        (key, desc) for key, desc in REQUIRED_ENV_KEYS if not env_map.get(key, "").strip()
    ]

    if missing_required and not interactive:
        keys = ", ".join(key for key, _ in missing_required)
        raise SystemExit(f"Missing required configuration for non-interactive startup: {keys}")

    if missing_required:
        print("")
        print("=" * 60)
        print("Missing configuration. Please enter values below:")
        print("(Ctrl+C to cancel)")
        print("=" * 60)
        for key, desc in missing_required:
            value = prompt_value(key, desc)
            if not value:
                raise SystemExit(f"{key} is required.")
            updates[key] = value
            env_map[key] = value

    if not env_map.get("ADMIN_TOKEN", "").strip():
        token = secrets.token_urlsafe(16)
        updates["ADMIN_TOKEN"] = token
        env_map["ADMIN_TOKEN"] = token
        print("ADMIN_TOKEN generated.")

    if updates:
        update_dotenv(updates)
        for key, value in updates.items():
            os.environ[key] = value
        print("Saved configuration to .env.")

    return updates


def run_preflight(*, interactive: bool) -> dict[str, object]:
    os.chdir(ROOT)
    os.environ["BIAOGE_ROOT"] = str(ROOT)

    ensure_venv()
    ensure_env_file()
    ensure_workflows_local_config()

    env_map = read_dotenv()
    normalize_env_paths(env_map)
    repair_workflow_config_path(env_map)
    env_map = read_dotenv()
    normalize_workflow_api_paths(env_map)

    host, port = parse_port(env_map)
    check_port_available(host, port)
    ensure_required_config(env_map, interactive=interactive)
    env_map = read_dotenv()
    check_socks_proxy_dependency(env_map)

    admin_token = env_map.get("ADMIN_TOKEN", "").strip()
    print("")
    print("Preflight OK.")
    print(f"Config page: http://{host}:{port}/admin/config?token={admin_token}")

    return {
        "root": str(ROOT),
        "venv_python": str(venv_python()),
        "host": host,
        "port": port,
        "admin_token": admin_token,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check and repair biaoge bot startup configuration.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--interactive", action="store_true", help="Prompt for missing required configuration.")
    mode.add_argument("--non-interactive", action="store_true", help="Fail fast when required configuration is missing.")
    args = parser.parse_args()
    run_preflight(interactive=args.interactive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
