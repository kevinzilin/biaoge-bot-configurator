from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import MutableMapping


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_bundle_path(raw: str, *, root: Path) -> Path:
    expanded = os.path.expandvars(raw.strip().strip('"').strip("'"))
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def configure_tls_ca_bundle(
    *,
    root: str | Path | None = None,
    environ: MutableMapping[str, str] | None = None,
    logger: logging.Logger | None = None,
) -> str | None:
    env = environ if environ is not None else os.environ
    base = Path(root).resolve() if root is not None else _project_root()
    log = logger or logging.getLogger(__name__)

    explicit_key = ""
    configured = ""
    for key in ("BIAOGE_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE"):
        value = (env.get(key) or "").strip()
        if value:
            explicit_key = key
            configured = value
            break

    bundle: Path | None = None
    source = ""
    if configured:
        candidate = _resolve_bundle_path(configured, root=base)
        if candidate.exists() and candidate.is_file():
            bundle = candidate
            source = "configured"
        else:
            log.warning("TLS CA bundle not found, ignoring: %s", str(candidate))

    if bundle is None:
        try:
            import certifi

            candidate = Path(certifi.where()).resolve()
            if candidate.exists() and candidate.is_file():
                bundle = candidate
                source = "certifi"
        except Exception as exc:
            log.warning("certifi CA bundle unavailable: %s", exc)

    if bundle is None:
        return None

    value = str(bundle)
    force_configured = bool(explicit_key) and env.get("BIAOGE_EFFECTIVE_CA_BUNDLE_SOURCE") == "certifi"
    if explicit_key == "BIAOGE_CA_BUNDLE" or force_configured:
        env["REQUESTS_CA_BUNDLE"] = value
        env["SSL_CERT_FILE"] = value
        env["CURL_CA_BUNDLE"] = value
    else:
        env.setdefault("REQUESTS_CA_BUNDLE", value)
        env.setdefault("SSL_CERT_FILE", value)
        env.setdefault("CURL_CA_BUNDLE", value)
    env["BIAOGE_EFFECTIVE_CA_BUNDLE"] = value
    if source:
        env["BIAOGE_EFFECTIVE_CA_BUNDLE_SOURCE"] = source
    return value
