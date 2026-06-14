from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def venv_python() -> Path:
    if os.name == "nt":
        return ROOT / ".venv" / "Scripts" / "python.exe"
    return ROOT / ".venv" / "bin" / "python"


def ensure_python_version() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10+ is required.")
    print(f"Python OK: {sys.version.split()[0]} ({sys.executable})")


def ensure_env_file() -> None:
    env_path = ROOT / ".env"
    if env_path.exists():
        return
    example = ROOT / ".env.example"
    if example.exists():
        env_path.write_text(example.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
        print(".env created from .env.example.")
    else:
        env_path.write_text("", encoding="utf-8")
        print(".env created [empty].")


def run(cmd: list[str]) -> None:
    print(" ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(ROOT), check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install biaoge bot dependencies for the current platform.")
    parser.add_argument("--skip-pip", action="store_true", help="Create files/venv only; do not install Python packages.")
    args = parser.parse_args()

    ensure_python_version()
    ensure_env_file()

    py = venv_python()
    if not py.exists():
        print("Creating virtual env [.venv] ...")
        run([sys.executable, "-m", "venv", str(ROOT / ".venv")])

    if not args.skip_pip:
        print("Installing dependencies ...")
        run([str(py), "-m", "pip", "install", "--upgrade", "pip"])
        run([str(py), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])

    next_cmd = "start.cmd" if os.name == "nt" else "./start.sh"
    print("")
    print(f"Done. Next: run {next_cmd} to launch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
