from __future__ import annotations

import argparse
import os
import subprocess
import sys
import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from biaoge_bot.logging_setup import append_runtime_log, daily_log_path

LOG_PATH = daily_log_path(ROOT)


def log_failure(message: str) -> None:
    append_runtime_log(ROOT, message.rstrip(), stamp=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run biaoge bot preflight checks and launch supervisor.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--interactive", action="store_true", help="Prompt for missing required configuration.")
    mode.add_argument("--non-interactive", action="store_true", help="Fail fast when required configuration is missing.")
    parser.add_argument("--preflight-only", action="store_true", help="Run checks without starting supervisor.")
    args = parser.parse_args()

    try:
        sys.dont_write_bytecode = True
        import preflight

        info = preflight.run_preflight(interactive=args.interactive)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        message = str(exc.code) if exc.code not in (None, 0) else f"Preflight failed with exit code {code}."
        log_failure(f"[launch] preflight failed: {message}")
        if message:
            print(message, file=sys.stderr)
        return code
    except Exception:
        message = traceback.format_exc()
        log_failure(f"[launch] unexpected preflight error:\n{message}")
        print(message, file=sys.stderr)
        return 1
    if args.preflight_only:
        return 0

    supervisor = ROOT / "scripts" / "supervisor.py"
    if not supervisor.exists():
        raise SystemExit(f"Missing file: {supervisor}")

    print("")
    print("Starting biaoge_bot with supervisor ...")
    print(f"Log file: {LOG_PATH}")
    os.chdir(ROOT)
    return subprocess.run([str(info["venv_python"]), str(supervisor)]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
