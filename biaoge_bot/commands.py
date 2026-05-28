from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Command:
    name: str
    args: dict[str, Any]


_SPACE = re.compile(r"\s+")
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")

def _split_tokens(text: str) -> list[str]:
    s = str(text or "")
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if quote is not None:
            if ch == quote:
                quote = None
                i += 1
                continue
            if ch == "\\" and i + 1 < n and s[i + 1] in (quote, "\\"):
                buf.append(s[i + 1])
                i += 2
                continue
            buf.append(ch)
            i += 1
            continue

        if ch in ('"', "'", "`"):
            quote = ch
            i += 1
            continue

        if ch.isspace():
            if buf:
                out.append("".join(buf))
                buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1
    if buf:
        out.append("".join(buf))
    return out


def _parse_kv(tok: str) -> tuple[str, str] | None:
    if "=" not in tok:
        return None
    k, v = tok.split("=", 1)
    k = k.strip()
    v = v.strip()
    if not k:
        return None
    return k, v


def _normalize_text(text: str) -> str:
    s = str(text or "")
    if not s:
        return ""
    s = s.replace("\u00a0", " ")
    s = _ZERO_WIDTH_RE.sub("", s)
    s = s.replace("：", ":").replace("／", "/").replace("∕", "/")
    return s


def _normalize_command_prefix(token: str) -> str:
    s = str(token or "")
    if s.startswith("::/"):
        return s
    if s.startswith("：：/"):
        return "::/" + s[3:]
    if s.startswith("::／"):
        return "::/" + s[3:]
    if s.startswith("：：／"):
        return "::/" + s[4:]
    return s


def parse_message_text(text: str) -> Command | None:
    text = _normalize_text(text).strip()
    parts0 = _split_tokens(text)
    if not parts0:
        return None
    idx = None
    for i, p in enumerate(parts0):
        p2 = _normalize_command_prefix(p)
        if p.startswith("/"):
            idx = i
            break
        if p2.startswith("::/") and len(p2) > 3:
            idx = i
            break
    if idx is None:
        return None
    parts = parts0[idx:]
    if not parts:
        return None
    first = _normalize_command_prefix(parts[0])
    if first.startswith("::/"):
        parts = [first[2:]] + parts[1:]

    head = parts[0].lstrip("/")
    if head in ("help", "h"):
        return Command(name="help", args={})
    if head in ("ids", "whoami", "where"):
        return Command(name="ids", args={})
    if head in ("botid", "bot", "botinfo"):
        return Command(name="botid", args={})
    if head == "panel":
        return Command(name="panel", args={})
    if head == "run_default":
        return Command(name="run_default", args={})

    if head in ("run", "wf", "batch", "drain", "stop_queue", "reset", "reset_table", "cb"):
        args: dict[str, Any] = {}
        rest = parts[1:]
        if head in ("wf", "batch", "drain", "stop_queue") and rest:
            args["workflow"] = rest[0]
            rest = rest[1:]

        for tok in rest:
            kv = _parse_kv(tok)
            if kv:
                args[kv[0]] = kv[1]
        return Command(name=head, args=args)

    return Command(name=head, args={})
