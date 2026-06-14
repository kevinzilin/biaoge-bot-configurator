from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "未加密源码"
MODULES = [
    (SOURCE_ROOT / "license_guard.py", ROOT / "biaoge_bot", "license_guard"),
    (SOURCE_ROOT / "modules" / "bitable.py", ROOT / "biaoge_bot" / "modules", "bitable"),
    (SOURCE_ROOT / "modules" / "drive.py", ROOT / "biaoge_bot" / "modules", "drive"),
    (SOURCE_ROOT / "modules" / "bitable_logic.py", ROOT / "biaoge_bot" / "modules", "bitable_logic"),
    (SOURCE_ROOT / "modules" / "bitable_trigger.py", ROOT / "biaoge_bot" / "modules", "bitable_trigger"),
    (SOURCE_ROOT / "modules" / "bitable_writeback.py", ROOT / "biaoge_bot" / "modules", "bitable_writeback"),
]


def _fixed_extension() -> str:
    return ".pyd" if os.name == "nt" else ".so"


def _candidate_outputs(output_dir: Path, module_name: str) -> list[Path]:
    ext = _fixed_extension()
    candidates = list(output_dir.glob(f"{module_name}.cp*{ext}"))
    candidates.extend(p for p in output_dir.glob(f"{module_name}*{ext}") if p.name != f"{module_name}{ext}")
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)


def _run(cmd: list[str], *, env: dict[str, str]) -> None:
    print(" ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(ROOT), env=env, check=True)


def build_one(
    *,
    python_exe: str,
    source: Path,
    output_dir: Path,
    module_name: str,
    nuitka_args: list[str],
    clean_build_dirs: bool,
    env: dict[str, str],
) -> None:
    if not source.exists():
        raise FileNotFoundError(f"source file not found: {source}")
    output_dir.mkdir(parents=True, exist_ok=True)
    fixed = output_dir / f"{module_name}{_fixed_extension()}"
    cmd = [
        python_exe,
        "-m",
        "nuitka",
        "--module",
        str(source),
        f"--output-dir={output_dir}",
        "--nofollow-imports",
        "--assume-yes-for-downloads",
        "--no-pyi-file",
        *nuitka_args,
    ]
    _run(cmd, env=env)
    candidates = _candidate_outputs(output_dir, module_name)
    if not candidates:
        raise RuntimeError(f"Nuitka output not found for {module_name} in {output_dir}")
    built = candidates[0]
    if built.resolve() != fixed.resolve():
        shutil.copy2(built, fixed)
        built.unlink()
    if clean_build_dirs:
        build_dir = output_dir / f"{module_name}.build"
        if build_dir.exists():
            shutil.rmtree(build_dir)
    print(f"output: {fixed}")


def default_python() -> str:
    venv = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if venv.exists():
        return str(venv)
    return sys.executable


def main() -> int:
    parser = argparse.ArgumentParser(description="Build protected biaoge_bot modules for the current platform.")
    parser.add_argument("--python", default=default_python(), help="Python executable with nuitka installed.")
    parser.add_argument("--clean-build-dirs", action="store_true", help="Remove Nuitka *.build directories after successful builds.")
    parser.add_argument("--check-only", action="store_true", help="Print environment and Nuitka version without building.")
    parser.add_argument("--nuitka-arg", action="append", default=[], help="Extra argument passed to Nuitka. Repeat as needed.")
    args = parser.parse_args()

    env = os.environ.copy()
    env.setdefault("BIAOGE_ROOT", str(ROOT))
    env.setdefault("NUITKA_CACHE_DIR", str(ROOT / "python_3.12_代码加密" / ".nuitka_cache"))
    env.setdefault("PYTHONPYCACHEPREFIX", str(ROOT / "python_3.12_代码加密" / ".pycache"))
    Path(env["NUITKA_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(env["PYTHONPYCACHEPREFIX"]).mkdir(parents=True, exist_ok=True)

    print(f"platform: {platform.platform()}")
    print(f"python: {args.python}")
    print(f"project root: {ROOT}")
    print(f"extension: {_fixed_extension()}")

    if args.check_only:
        _run([args.python, "-m", "nuitka", "--version"], env=env)
        return 0

    for source, output_dir, module_name in MODULES:
        build_one(
            python_exe=args.python,
            source=source,
            output_dir=output_dir,
            module_name=module_name,
            nuitka_args=list(args.nuitka_arg or []),
            clean_build_dirs=bool(args.clean_build_dirs),
            env=env,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
