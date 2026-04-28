"""
Process Guard - Singleton Bot Lock
====================================
Prevents duplicate bot instances from running simultaneously.

Uses a PID lock file with stale process detection:
- On startup, checks for existing lock
- If lock exists and process is alive → abort (duplicate)
- If lock exists but process is dead → clean stale lock and take over
- Writes current PID to lock file
- On shutdown, removes lock file

Works cross-platform (Windows + Linux).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent
_DEFAULT_LOCK_FILE = _PROJECT_ROOT / "bot.pid"


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    if pid <= 0:
        return False

    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == STILL_ACTIVE
                return False
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we can't signal it
            return True
        except Exception:
            return False


def _is_python_bot_process(pid: int) -> bool:
    """Verify the PID is actually a Python bot process, not a recycled PID.

    On Windows, we check via CIM (fast). On Linux, we check /proc.
    Falls back to simple alive check if platform query fails.
    """
    if not _is_process_alive(pid):
        return False

    if sys.platform == "win32":
        try:
            import subprocess

            result = subprocess.run(
                [
                    "powershell",
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    f'(Get-CimInstance Win32_Process -Filter "ProcessId={pid}" '
                    f"| Select-Object -First 1).CommandLine",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            cmdline = (result.stdout or "").strip()
            if not cmdline:
                return False
            # Must be a python process running from this project
            project_hint = str(_PROJECT_ROOT).lower()
            return "python" in cmdline.lower() and project_hint in cmdline.lower()
        except Exception as exc:
            logger.debug("Failed to inspect Windows process command line for PID %s: %s", pid, exc)
    else:
        try:
            cmdline_path = Path(f"/proc/{pid}/cmdline")
            if cmdline_path.exists():
                cmdline = cmdline_path.read_bytes().decode("utf-8", errors="replace")
                project_hint = str(_PROJECT_ROOT).lower()
                return "python" in cmdline.lower() and project_hint in cmdline.lower()
        except Exception as exc:
            logger.debug("Failed to inspect /proc command line for PID %s: %s", pid, exc)

    # Fallback: process is alive but we can't verify identity
    return _is_process_alive(pid)


def _read_lock_file(lock_path: Path) -> Optional[Dict[str, Any]]:
    """Read and parse bot.pid lock file. Returns None if unreadable."""
    try:
        content = lock_path.read_text(encoding="utf-8").strip()
        if not content:
            return None

        # Try JSON format first
        if content.startswith("{"):
            return json.loads(content)

        # Legacy format: just a PID number
        pid = int(content.split()[0])
        return {"pid": pid, "started_at": None, "source": "unknown"}
    except (json.JSONDecodeError, ValueError, OSError):
        return None


def _write_lock_file(lock_path: Path, source: str = "main") -> None:
    """Write lock file with current PID and metadata."""
    payload = {
        "pid": os.getpid(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": source,
        "python": sys.executable,
    }
    try:
        lock_path.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.error("Failed to write lock file %s: %s", lock_path, exc)


def _remove_lock_file(lock_path: Path) -> None:
    """Remove lock file if it belongs to current process."""
    try:
        info = _read_lock_file(lock_path)
        if info and info.get("pid") == os.getpid():
            lock_path.unlink(missing_ok=True)
            logger.info("Lock file removed: %s", lock_path)
        elif info:
            logger.debug(
                "Lock file %s belongs to PID %s (we are %s) — leaving it",
                lock_path,
                info.get("pid"),
                os.getpid(),
            )
    except OSError as exc:
        logger.debug("Failed to remove lock file %s: %s", lock_path, exc)


def _force_kill_pid(pid: int, label: str = "process") -> None:
    """Send SIGTERM then SIGKILL to a process."""
    if pid <= 0 or not _is_process_alive(pid):
        return
    logger.info("Terminating %s (PID %s) ...", label, pid)
    try:
        if sys.platform == "win32":
            import subprocess as _sp

            _sp.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=10)
        else:
            os.kill(pid, signal.SIGTERM)
            # Give it a short grace period then force kill
            for _ in range(10):
                if not _is_process_alive(pid):
                    return
                time.sleep(0.3)
            os.kill(pid, signal.SIGKILL)
    except Exception as exc:
        logger.warning("Failed to kill PID %s: %s", pid, exc)


def kill_stale_bot_process(lock_path: Path = _DEFAULT_LOCK_FILE) -> bool:
    """Kill a stale bot process if the lock file references a dead or stuck process.

    Returns True if a stale process was cleaned up or no lock existed.
    Returns False if a healthy process is running (do not proceed).
    """
    info = _read_lock_file(lock_path)
    if info is None:
        return True  # No lock → safe to proceed

    pid = info.get("pid", 0)
    if not _is_process_alive(pid):
        logger.info(
            "Stale lock file found (PID %s is dead) — cleaning up",
            pid,
        )
        try:
            lock_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to remove stale lock file %s: %s", lock_path, exc)
        return True

    if not _is_python_bot_process(pid):
        logger.warning(
            "Lock file PID %s is alive but NOT a bot process (recycled PID) — cleaning lock",
            pid,
        )
        try:
            lock_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to remove recycled-PID lock file %s: %s", lock_path, exc)
        return True

    # Process is alive and is actually a bot → try graceful termination
    logger.warning("Existing bot process PID %s is still running", pid)
    return False


def acquire_bot_lock(
    lock_path: Path = _DEFAULT_LOCK_FILE,
    source: str = "main",
    kill_stale: bool = True,
) -> bool:
    """Try to acquire the singleton bot lock.

    Args:
        lock_path: Path to the PID lock file
        source: Identifier for what is acquiring the lock (main/watchdog)
        kill_stale: Whether to auto-clean stale locks

    Returns:
        True if lock was acquired (safe to start bot).
        False if another bot is already running.
    """
    if lock_path.exists():
        info = _read_lock_file(lock_path)

        if info is None:
            # Corrupt lock file — remove and proceed
            logger.warning("Corrupt lock file %s — removing", lock_path)
            try:
                lock_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Failed to remove corrupt lock file %s: %s", lock_path, exc)
        else:
            pid = info.get("pid", 0)

            # Same PID (re-entry) → allow
            if pid == os.getpid():
                _write_lock_file(lock_path, source)
                return True

            if _is_process_alive(pid) and _is_python_bot_process(pid):
                started = info.get("started_at", "unknown")
                src = info.get("source", "unknown")
                logger.warning(
                    "Killing existing bot instance to take over — " "PID=%s source=%s started=%s",
                    pid,
                    src,
                    started,
                )
                _force_kill_pid(pid, "Trading Bot")
                # Wait a moment for the process to fully terminate
                for _ in range(20):
                    if not _is_process_alive(pid):
                        break
                    time.sleep(0.3)
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("Failed to remove prior lock file %s after takeover: %s", lock_path, exc)

            # Stale lock
            if kill_stale:
                logger.info(
                    "Cleaning stale lock file (PID %s is no longer a valid bot process)",
                    pid,
                )
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("Failed to remove stale lock file %s during acquire: %s", lock_path, exc)
            else:
                return False

    # Write new lock
    _write_lock_file(lock_path, source)
    logger.info(
        "Bot lock acquired — PID=%s source=%s lock=%s",
        os.getpid(),
        source,
        lock_path,
    )
    return True


def release_bot_lock(lock_path: Path = _DEFAULT_LOCK_FILE) -> None:
    """Release the bot lock file (call on shutdown)."""
    _remove_lock_file(lock_path)


def get_lock_status(lock_path: Path = _DEFAULT_LOCK_FILE) -> Dict[str, Any]:
    """Get status of the current lock file for diagnostics."""
    if not lock_path.exists():
        return {"locked": False, "lock_file": str(lock_path)}

    info = _read_lock_file(lock_path)
    if info is None:
        return {"locked": False, "lock_file": str(lock_path), "corrupt": True}

    pid = info.get("pid", 0)
    alive = _is_process_alive(pid)
    is_bot = _is_python_bot_process(pid) if alive else False

    return {
        "locked": alive and is_bot,
        "stale": alive and not is_bot,
        "dead": not alive,
        "lock_file": str(lock_path),
        "pid": pid,
        "source": info.get("source"),
        "started_at": info.get("started_at"),
        "is_current_process": pid == os.getpid(),
    }
