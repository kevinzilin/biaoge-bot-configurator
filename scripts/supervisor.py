from __future__ import annotations

import ctypes
import os
import platform
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from biaoge_bot.logging_setup import append_runtime_log, daily_log_path
from biaoge_bot.network import configure_local_proxy_bypass
from biaoge_bot.tls import configure_tls_ca_bundle

LOG_DIR = ROOT / "logs"
LOCK_PATH = LOG_DIR / "runtime" / "biaoge_bot.supervisor.pid"
LEGACY_LOCK_PATH = ROOT / "tmp" / "biaoge_bot.supervisor.pid"
_ACTIVE_LOCK_PATH = LOCK_PATH


def _venv_python() -> Path:
    if os.name == "nt":
        return ROOT / ".venv" / "Scripts" / "python.exe"
    return ROOT / ".venv" / "bin" / "python"


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
    except Exception:
        pass
    return False


def _acquire_lock() -> int:
    global _ACTIVE_LOCK_PATH
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LEGACY_LOCK_PATH.exists():
        try:
            old_pid = int(LEGACY_LOCK_PATH.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            old_pid = 0
        if _pid_exists(old_pid):
            raise SystemExit(f"supervisor already running: pid={old_pid}")
        try:
            LEGACY_LOCK_PATH.unlink()
        except Exception:
            pass
    if LOCK_PATH.exists():
        try:
            old_pid = int(LOCK_PATH.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            old_pid = 0
        if _pid_exists(old_pid):
            raise SystemExit(f"supervisor already running: pid={old_pid}")
        try:
            LOCK_PATH.unlink()
        except Exception:
            pass
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            old_pid = int(LOCK_PATH.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            old_pid = 0
        if _pid_exists(old_pid):
            raise SystemExit(f"supervisor already running: pid={old_pid}")
        fd = os.open(str(LOCK_PATH), os.O_WRONLY | os.O_TRUNC)
    _ACTIVE_LOCK_PATH = LOCK_PATH
    os.write(fd, str(os.getpid()).encode("utf-8"))
    return fd


def _release_lock(fd: int) -> None:
    try:
        os.close(fd)
    except Exception:
        pass
    try:
        if _ACTIVE_LOCK_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
            _ACTIVE_LOCK_PATH.unlink()
    except Exception:
        pass


def _write(text: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {text.rstrip()}\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    append_runtime_log(ROOT, line, stamp=False)


def _tee_child_output(child: subprocess.Popen[str]) -> int:
    assert child.stdout is not None
    for line in child.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        append_runtime_log(ROOT, line, stamp=False)
    return child.wait()


def _prevent_windows_sleep(enable: bool) -> None:
    if os.name != "nt":
        return
    try:
        continuous = 0x80000000
        system_required = 0x00000001
        awaymode_required = 0x00000040
        flags = continuous | system_required | awaymode_required if enable else continuous
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def _start_inhibitor(child: subprocess.Popen[str]) -> subprocess.Popen[bytes] | None:
    if os.environ.get("BOT_PREVENT_SLEEP", "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    system = platform.system().lower()
    try:
        if system == "darwin":
            return subprocess.Popen(["caffeinate", "-dimsu", "-w", str(child.pid)])
        if system == "linux":
            return subprocess.Popen(["systemd-inhibit", "--what=sleep", "--why=biaoge bot running", "sleep", "infinity"])
        if os.name == "nt":
            _prevent_windows_sleep(True)
    except FileNotFoundError:
        _write("sleep inhibitor command not found; continuing without sleep prevention")
    except Exception as exc:
        _write(f"sleep inhibitor failed: {exc}")
    return None


def _stop_inhibitor(proc: subprocess.Popen[bytes] | None) -> None:
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass


def _restart_delay(previous: int) -> int:
    max_delay = 60
    try:
        max_delay = max(5, int(os.environ.get("BOT_RESTART_DELAY_MAX_SECONDS", "60")))
    except Exception:
        pass
    return min(max(previous * 2, 5), max_delay)


def main() -> int:
    py = _venv_python()
    if not py.exists():
        sys.stderr.write(f"virtualenv python not found: {py}\n")
        return 1

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = daily_log_path(ROOT)
    lock_fd = _acquire_lock()
    child: subprocess.Popen[str] | None = None
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping, child
        stopping = True
        if child and child.poll() is None:
            try:
                child.terminate()
            except Exception:
                pass

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    delay = 5
    try:
        while not stopping:
            configure_local_proxy_bypass()
            configure_tls_ca_bundle(root=ROOT)
            env = os.environ.copy()
            env.setdefault("BIAOGE_ROOT", str(ROOT))
            env.setdefault("PYTHONUNBUFFERED", "1")
            env.setdefault("PYTHONIOENCODING", "utf-8")
            env.setdefault("BIAOGE_SUPERVISOR_CAPTURE", "1")
            cmd = [str(py), "-m", "biaoge_bot.main"]
            _write("log file: " + str(log_file))
            _write("starting: " + " ".join(cmd))
            child = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            inhibitor = _start_inhibitor(child)
            rc = _tee_child_output(child)
            _stop_inhibitor(inhibitor)
            if os.name == "nt":
                _prevent_windows_sleep(False)
            if stopping:
                break
            _write(f"child exited with code {rc}; restarting in {delay}s")
            time.sleep(delay)
            delay = _restart_delay(delay)
    finally:
        if os.name == "nt":
            _prevent_windows_sleep(False)
        _release_lock(lock_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
