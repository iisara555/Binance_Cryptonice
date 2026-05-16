#!/usr/bin/env python3
"""
Crypto Bot Watchdog
- Checks if bot is alive by verifying DB prices are recent
- Sends Telegram alert if down
- Auto-restarts bot (respects singleton lock)
- Run via cron: every 5 minutes

SECURITY: All sensitive credentials must be loaded from environment variables.
          NEVER hardcode API keys or tokens in this file.
"""

import io
import logging
import logging.handlers
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

from project_paths import PROJECT_ROOT, resolve_project_python

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ── Directory Configuration ──────────────────────────────────────────────────
BOT_DIR = PROJECT_ROOT
BOT_SCRIPT = BOT_DIR / "main.py"
DB_PATH = BOT_DIR / "crypto_bot.db"
LOG_FILE = BOT_DIR / "watchdog.log"
PID_FILE = BOT_DIR / "bot.pid"

# Import process guard for lock-aware checks
sys.path.insert(0, str(BOT_DIR))
try:
    from process_guard import _is_process_alive, get_lock_status
except ImportError:
    # Fallback if process_guard not available
    def get_lock_status(lock_path=None):
        return {"locked": False}

    def _is_process_alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


# ── Telegram Configuration (from environment) ──────────────────────────────
# MUST be set in .env file — bot will exit if not found
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def _validate_telegram_config():
    """Validate Telegram config exists - fail-fast if missing."""
    if not TELEGRAM_BOT_TOKEN:
        print("[ERROR] TELEGRAM_BOT_TOKEN not set in environment")
        print("        Set this in your .env file before running watchdog")
        sys.exit(1)
    if not TELEGRAM_CHAT_ID:
        print("[ERROR] TELEGRAM_CHAT_ID not set in environment")
        print("        Set this in your .env file before running watchdog")
        sys.exit(1)


# ── How old can last price be before we assume bot is dead (minutes) ───────
STALE_THRESHOLD_MINUTES = 5

# ── Setup proper rotating log instead of manual file append ────────────────
_wd_logger = logging.getLogger("watchdog")
_wd_logger.setLevel(logging.INFO)
_wd_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE,
    maxBytes=10 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
_wd_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
_wd_logger.addHandler(_wd_handler)
_wd_console = logging.StreamHandler()
_wd_console.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
_wd_logger.addHandler(_wd_console)


def log(msg):
    _wd_logger.info(msg)


def send_telegram(msg):
    """Send message via Telegram. Uses environment variables only."""
    # Validate config before sending
    _validate_telegram_config()

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            log(f"Telegram send failed: {response.status_code} - {response.text}")
    except requests.RequestException as e:
        log(f"Telegram error: {e}")


def get_last_price_time():
    """Get timestamp of most recent price in DB."""
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        cursor = conn.cursor()
        cursor.execute("PRAGMA busy_timeout=5000")
        # Keep watchdog reads compatible with bot's WAL write pattern.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("SELECT MAX(timestamp) FROM prices")
        result = cursor.fetchone()[0]
        conn.close()
        if result:
            return datetime.fromisoformat(result)
        return None
    except (sqlite3.Error, ValueError) as e:
        log(f"DB error: {e}")
        return None


def is_bot_process_running():
    """Check if bot process is running using process guard lock."""
    lock_status = get_lock_status(Path(PID_FILE))
    if lock_status.get("locked"):
        log(f"Bot process is running (PID {lock_status.get('pid')}, source={lock_status.get('source')})")
        return True

    # Also check if PID is alive but stale (process guard handles recycled PIDs)
    if lock_status.get("stale"):
        log(f"PID {lock_status.get('pid')} exists but is NOT a bot process (recycled PID)")
        return False

    if lock_status.get("dead"):
        log(f"PID {lock_status.get('pid')} from lock file is dead")
        return False

    return False


def start_bot():
    """Start the bot process. The bot itself acquires the singleton lock via process_guard."""
    log("Bot appears dead. Starting bot...")

    # Check lock before starting — if locked, another instance is alive
    lock_status = get_lock_status(Path(PID_FILE))
    if lock_status.get("locked"):
        log(f"Cannot start: another bot instance is still running (PID {lock_status.get('pid')})")
        return False

    try:
        project_python = resolve_project_python(BOT_DIR)
        if not project_python:
            raise FileNotFoundError(f"Could not find a project Python executable under {BOT_DIR}")

        # Start bot in background — the bot's main() will acquire its own lock
        if sys.platform == "win32":
            DETACHED_PROCESS = 0x00000008
            devnull = open(os.devnull, "w")
            try:
                proc = subprocess.Popen(
                    [str(project_python), str(BOT_SCRIPT)],
                    cwd=str(BOT_DIR),
                    creationflags=DETACHED_PROCESS,
                    stdout=devnull,
                    stderr=subprocess.STDOUT,
                )
            finally:
                devnull.close()
        else:
            devnull = open("/dev/null", "w")
            try:
                proc = subprocess.Popen(
                    [str(project_python), str(BOT_SCRIPT)],
                    cwd=str(BOT_DIR),
                    stdout=devnull,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            finally:
                devnull.close()

        log(f"Bot started successfully (child PID {proc.pid})")
        send_telegram(f"🔄 Crypto Bot auto-restarted by Watchdog (PID {proc.pid})")
        return True
    except (OSError, subprocess.SubprocessError) as e:
        log(f"Failed to start bot: {e}")
        send_telegram(f"⚠️ Crypto Bot restart FAILED: {e}")
        return False


def main():
    """Main watchdog check."""
    print(f"Watchdog check at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # Validate Telegram config exists
    _validate_telegram_config()

    # Check last price time
    last_price = get_last_price_time()
    if last_price is None:
        log("No prices found in DB")
        send_telegram("⚠️ Crypto Bot may be down: No prices in database")
        if not is_bot_process_running():
            start_bot()
        return

    age_minutes = (datetime.now() - last_price).total_seconds() / 60
    log(f"Last price: {last_price} ({age_minutes:.1f} minutes ago)")

    if age_minutes > STALE_THRESHOLD_MINUTES:
        log(f"Price data STALE ({age_minutes:.1f} min > {STALE_THRESHOLD_MINUTES} min threshold)")
        send_telegram(f"⚠️ Crypto Bot may be down: Last price {age_minutes:.0f} minutes ago")

        if not is_bot_process_running():
            start_bot()
        else:
            log("Bot process running but no prices — may be stuck")
    else:
        log("Bot appears healthy")


if __name__ == "__main__":
    main()
