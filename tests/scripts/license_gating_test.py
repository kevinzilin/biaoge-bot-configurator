from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from biaoge_bot.license_guard import get_device_code
from biaoge_bot.main import build_context


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _rsa_emsa_pkcs1_v1_5_encode_sha256(message: bytes, k: int) -> bytes:
    digest = hashlib.sha256(message).digest()
    digest_info_prefix = bytes.fromhex("3031300d060960864801650304020105000420")
    t = digest_info_prefix + digest
    if k < len(t) + 11:
        raise ValueError("key too small")
    ps = b"\xff" * (k - len(t) - 3)
    return b"\x00\x01" + ps + b"\x00" + t


def _rsa_sign_pkcs1_v1_5_sha256(n: int, d: int, message: bytes) -> bytes:
    k = (n.bit_length() + 7) // 8
    em = _rsa_emsa_pkcs1_v1_5_encode_sha256(message, k)
    m = int.from_bytes(em, "big", signed=False)
    s = pow(m, d, n)
    return s.to_bytes(k, "big", signed=False)


def _read_private_key() -> tuple[int, int]:
    p = Path(__file__).resolve().parents[2] / "AnySwitch_license_tools" / "private_key.json"
    key = json.loads(p.read_text(encoding="utf-8"))
    n_hex = str(key.get("n") or "").strip()
    d_hex = str(key.get("d") or "").strip()
    if not (n_hex and d_hex):
        raise RuntimeError("private_key.json missing n/d")
    return int(n_hex, 16), int(d_hex, 16)


def _build_license_text(*, device_code: str, exp_ts: int) -> str:
    payload = {"v": 1, "alg": "RS256", "codes": [device_code], "exp": int(exp_ts)}
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    payload_b64 = _b64url_encode(payload_json.encode("utf-8"))
    n, d = _read_private_key()
    sig = _rsa_sign_pkcs1_v1_5_sha256(n, d, payload_b64.encode("utf-8"))
    sig_b64 = _b64url_encode(sig)
    return f"{payload_b64}.{sig_b64}"


def _capture_warning_logs(fn: Callable[[], object]) -> tuple[object, str]:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.WARNING)
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    root.handlers = [handler]
    root.setLevel(logging.WARNING)
    try:
        return fn(), buf.getvalue()
    finally:
        root.handlers = old_handlers
        root.setLevel(old_level)


def _write_env_file(path: Path, *, workflow_config_path: Path) -> None:
    lines = [
        "FEISHU_APP_ID=test_app_id",
        "FEISHU_APP_SECRET=test_app_secret",
        "BITABLE_MODE=auto",
        f"WORKFLOW_CONFIG_PATH={workflow_config_path}",
        "BOT_LOG_LEVEL=INFO",
        "CALLBACK_HOST=127.0.0.1",
        "CALLBACK_PORT=9901",
        "CALLBACK_PATH=/comfyui/callback",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_case(*, name: str, env_file: str, license_path: Path, expect_enabled: bool, expect_guidance: bool) -> None:
    os.environ["BIAOGE_LICENSE_PATH"] = str(license_path)
    ctx_obj, logs = _capture_warning_logs(lambda: build_context(env_file))
    ctx = ctx_obj
    read_enabled = bool(getattr(ctx, "bitable_mode").read_enabled)
    write_enabled = bool(getattr(ctx, "bitable_mode").write_enabled)
    bitables_len = len(getattr(ctx, "bitables") or {})
    drive_enabled = getattr(ctx, "drive") is not None

    if expect_enabled:
        assert read_enabled and write_enabled, f"{name}: bitable_mode should be enabled"
        assert bitables_len > 0, f"{name}: bitables should be enabled"
        assert drive_enabled, f"{name}: drive should be enabled"
    else:
        assert (not read_enabled) and (not write_enabled), f"{name}: bitable_mode should be disabled"
        assert bitables_len == 0, f"{name}: bitables should be disabled"
        assert not drive_enabled, f"{name}: drive should be disabled"

    if expect_guidance:
        assert "未检测到有效授权" in logs, f"{name}: should output guidance"
        assert "设备码:" in logs, f"{name}: should output device code"
        assert "请将 license.lic 放置到" in logs, f"{name}: should output license path instruction"
    else:
        assert "未检测到有效授权" not in logs, f"{name}: should not output guidance"

    out = {
        "case": name,
        "bitable_mode": {"read_enabled": read_enabled, "write_enabled": write_enabled},
        "bitables": list((getattr(ctx, "bitables") or {}).keys()),
        "drive": drive_enabled,
        "bitable_configs": list((getattr(ctx, "bitable_configs") or {}).keys()),
        "default_table_key": getattr(ctx, "default_table_key"),
    }
    print(json.dumps(out, ensure_ascii=False))
    if logs.strip():
        print(logs.strip())


def main() -> None:
    base = Path(__file__).resolve().parents[2] / "tmp" / "license_gating_test"
    base.mkdir(parents=True, exist_ok=True)

    cfg_path = base / "workflow_config.json"
    cfg_path.write_text(json.dumps({"bitable": {"app_token": "app_test", "table_id": "tbl_test"}}, ensure_ascii=False), encoding="utf-8")

    env_path = base / "license_gating.env"
    _write_env_file(env_path, workflow_config_path=cfg_path)

    dc = get_device_code()
    now = int(time.time())
    lic_valid = base / "valid.lic"
    lic_expired = base / "expired.lic"
    lic_valid.write_text(_build_license_text(device_code=dc, exp_ts=now + 86400), encoding="utf-8")
    lic_expired.write_text(_build_license_text(device_code=dc, exp_ts=now - 10), encoding="utf-8")

    _run_case(
        name="no_license",
        env_file=str(env_path),
        license_path=base / "missing.lic",
        expect_enabled=False,
        expect_guidance=True,
    )
    _run_case(
        name="valid_license",
        env_file=str(env_path),
        license_path=lic_valid,
        expect_enabled=True,
        expect_guidance=False,
    )
    _run_case(
        name="expired_license",
        env_file=str(env_path),
        license_path=lic_expired,
        expect_enabled=False,
        expect_guidance=True,
    )


if __name__ == "__main__":
    main()
