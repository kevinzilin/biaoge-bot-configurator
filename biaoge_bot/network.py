from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlparse
from typing import Iterable, MutableMapping


_LOCAL_PROXY_BYPASS_DEFAULTS = (
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
)


def _host_from_url_or_host(value: str) -> str:
    raw = str(value or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    if "://" in raw:
        parsed = urlparse(raw)
        return (parsed.hostname or "").strip().strip("[]")
    host = raw.split("/", 1)[0].strip().strip("[]")
    if host.count(":") == 1 and not host.startswith("["):
        host = host.rsplit(":", 1)[0]
    return host.strip()


def is_local_or_private_host(value: str) -> bool:
    host = _host_from_url_or_host(value).lower()
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return True
    if host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local)


def should_trust_env_proxy_for_url(url: str) -> bool:
    return not is_local_or_private_host(url)


def _split_no_proxy(value: str) -> list[str]:
    out: list[str] = []
    for item in str(value or "").split(","):
        item = item.strip()
        if not item:
            continue
        # httpx accepts ::1 in NO_PROXY, but bracketed [::1] is parsed as an
        # URL pattern with an invalid port and breaks every AsyncClient.
        if item == "[::1]":
            item = "::1"
        out.append(item)
    return out


def configure_local_proxy_bypass(
    values: Iterable[str] | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> str:
    env = environ if environ is not None else os.environ
    merged: list[str] = []
    seen: set[str] = set()

    for raw in (env.get("NO_PROXY") or env.get("no_proxy") or "",):
        for item in _split_no_proxy(raw):
            key = item.lower()
            if key not in seen:
                seen.add(key)
                merged.append(item)

    candidates = list(_LOCAL_PROXY_BYPASS_DEFAULTS)
    for value in values or ():
        host = _host_from_url_or_host(value)
        if host and is_local_or_private_host(host):
            candidates.append(host)

    for item in candidates:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            merged.append(item)

    out = ",".join(merged)
    env["NO_PROXY"] = out
    env["no_proxy"] = out
    return out
