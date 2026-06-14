from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path


LOG_PREFIX = "biaoge_bot"


def daily_log_path(root: str | Path, *, prefix: str = LOG_PREFIX) -> Path:
    root_path = Path(root)
    day = datetime.now().strftime("%Y-%m-%d")
    return root_path / "logs" / f"{prefix}-{day}.log"


def append_runtime_log(root: str | Path, text: str, *, stamp: bool = False) -> Path:
    path = daily_log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = text.rstrip("\n")
    if stamp:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {line}"
    with path.open("a", encoding="utf-8", errors="replace") as handle:
        handle.write(line + "\n")
    return path


class DailyFileHandler(logging.Handler):
    def __init__(self, root: str | Path, *, prefix: str = LOG_PREFIX) -> None:
        super().__init__()
        self.root = Path(root)
        self.prefix = prefix

    def emit(self, record: logging.LogRecord) -> None:
        try:
            append_runtime_log(self.root, self.format(record), stamp=False)
        except Exception:
            self.handleError(record)
