from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main() -> int:
    venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
    py = venv_py if venv_py.exists() else Path(sys.executable)
    launch = ROOT / "scripts" / "launch.py"
    os.chdir(ROOT)
    return subprocess.run([str(py), str(launch), "--interactive"]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
