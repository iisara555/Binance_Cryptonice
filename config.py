"""
Configuration for Binance Thailand Crypto Trading Bot.
Loads settings from environment variables (.env file).

SECURITY: All sensitive credentials must be loaded from environment variables.
          This module will CRITICALLY FAIL on startup if required env vars are missing.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from project_paths import PROJECT_ROOT

# Load .env file if python-dotenv is available
try:
    from dotenv import load_dotenv

    # Prefer the project-root .env so the bot stays portable when launched elsewhere.
    _env_paths = [
        PROJECT_ROOT / ".env",
        Path.cwd() / ".env",
    ]
    for env_path in _env_paths:
        if env_path.exists():
            load_dotenv(env_path, override=True)
            break
    else:
        # No .env file found - check if this is expected (for testing)
        if not os.environ.get("BINANCE_API_KEY"):
            print("[WARNING] No .env file found and BINANCE_API_KEY not set")
except ImportError:
    pass  # python-dotenv not installed, rely on system env vars


# ── Paths ────────────────────────────────────────────────────────────────────

ENV_FILE: Path = PROJECT_ROOT / ".env"


# ── System Objective ─────────────────────────────────────────────────────────
# Primary fitness metric for all trading decisions and future ML tuning.
# All sub-systems (risk, execution, signal) must optimize toward this objective.
SYSTEM_OBJECTIVE: str = "MAXIMIZE_NET_PROFIT"

# Minimum Risk:Reward ratio to approve a trade.
# TP distance must be >= MIN_RR * SL distance, otherwise trade is rejected.
MIN_RISK_REWARD_RATIO: float = 1.1  # ADJUSTED v2.1 - loosened for signal generation


def _get_env(key: str, default: Optional[str] = None) -> str:
    """Get environment variable with optional default.

    For CRITICAL env vars, pass default=None to enforce fail-fast.
    """
    value = os.environ.get(key)
    if value is not None:
        return value
    if default is not None:
        return default
    # CRITICAL: Env var not found and no default - raise error
    raise EnvironmentError(
        f"Required environment variable '{key}' is not set. "
        f"Please set this in your .env file before starting the bot."
    )


def _get_env_strict(key: str) -> str:
    """Get environment variable with STRICT validation - fails if missing.

    Use this for critical credentials that must be present.
    """
    value = os.environ.get(key, "").strip()
    if not value:
        raise EnvironmentError(
            f"CRITICAL: Environment variable '{key}' is not set or is empty.\n"
            f"Please ensure this is set in your .env file.\n"
            f"Bot startup aborted for security."
        )
    # Check for placeholder values
    placeholder_patterns = ["your_", "replace_this", "placeholder", "changeme"]
    value_lower = value.lower()
    for pattern in placeholder_patterns:
        if pattern in value_lower:
            raise EnvironmentError(
                f"CRITICAL: '{key}' appears to be a placeholder value ('{value}').\n"
                f"Please set the actual API key in your .env file.\n"
                f"Bot startup aborted for security."
            )
    return value


def _get_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except ValueError:
        return default


def _get_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, "").lower()
    if val in ("true", "1", "yes"):
        return True
    if val in ("false", "0", "no"):
        return False
    return default


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class BinanceConfig:
    api_key: str
    api_secret: str
    base_url: str
    default_symbol: str


@dataclass
class TradingConfig:
    min_order_amount: float
    default_order_type: str  # "limit" or "market"
    live_trading: bool


@dataclass
class BotConfig:
    log_level: str


# ── Config Instances ──────────────────────────────────────────────────────────


def _load_binance_config() -> BinanceConfig:
    """Load Binance Thailand config with strict validation."""
    try:
        api_key = _get_env_strict("BINANCE_API_KEY")
        api_secret = _get_env_strict("BINANCE_API_SECRET")
    except EnvironmentError:
        # Re-raise with more context
        print("\n" + "=" * 60)
        print("FATAL: Binance API credentials not configured!")
        print("=" * 60)
        print("\nTo fix this:")
        print("1. Create a .env file in the project root")
        print("2. Add the following lines:")
        print("   BINANCE_API_KEY=your_actual_api_key")
        print("   BINANCE_API_SECRET=your_actual_api_secret")
        print("\nGet your API keys from: https://www.binance.th")
        print("=" * 60 + "\n")
        raise

    return BinanceConfig(
        api_key=api_key,
        api_secret=api_secret,
        base_url=os.environ.get("BINANCE_BASE_URL", "https://api.binance.th"),
        default_symbol=os.environ.get("DEFAULT_SYMBOL", "BTCUSDT"),
    )


# Load config at module import - will CRITICAL FAIL if keys are missing
BINANCE = _load_binance_config()

TRADING = TradingConfig(
    min_order_amount=_get_float("MIN_ORDER_AMOUNT", 100.0),
    default_order_type=_get_env("DEFAULT_ORDER_TYPE", "limit"),
    live_trading=_get_bool("LIVE_TRADING", False),
)

BOT = BotConfig(
    log_level=os.environ.get("LOG_LEVEL", "INFO"),
)


# ── Validation ────────────────────────────────────────────────────────────────


def validate_config() -> tuple[list[str], list[str]]:
    """Return (critical_errors, warnings). Empty critical = all good.

    This function performs post-load validation and warnings.
    The actual env var loading already failed at import time if keys were missing.
    """
    critical = []
    warnings = []

    # These are already validated at import time, but we check anyway
    if not BINANCE.api_key:
        critical.append("BINANCE_API_KEY is empty (should not happen)")
    if not BINANCE.api_secret:
        critical.append("BINANCE_API_SECRET is empty (should not happen)")

    # Trading mode warnings
    if TRADING.live_trading:
        warnings.append("⚠️ LIVE_TRADING is enabled — real orders WILL be placed!")

    # Telegram config check (optional but recommended)
    telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not telegram_bot_token or not telegram_chat_id:
        warnings.append("Telegram notifications disabled (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set)")

    return critical, warnings


def check_env_file_exists() -> bool:
    """Check if .env file exists and warn if not."""
    if not ENV_FILE.exists():
        print(f"[WARNING] .env file not found at {ENV_FILE}")
        print("         Bot will use environment variables from system.")
        return False
    return True


# ── Runtime Validation ────────────────────────────────────────────────────────


def enforce_critical_config():
    """Enforce that critical configuration is present.

    Call this at bot startup before any trading operations.
    Raises SystemExit if configuration is invalid.
    """
    critical, warnings = validate_config()

    # Log warnings
    for warning in warnings:
        print(f"[WARNING] {warning}")

    # Check for critical errors (should not happen if module loaded successfully)
    if critical:
        print("\n[ERROR] Critical configuration errors:")
        for err in critical:
            print(f"  - {err}")
        print("\nBot startup aborted.")
        sys.exit(1)


# Auto-check on import if running as main script
if __name__ == "__main__":
    print("Binance Thailand Trading Bot Configuration")
    print("=" * 40)
    print(f"Project Root: {PROJECT_ROOT}")
    print(f"Env File: {ENV_FILE} (exists: {ENV_FILE.exists()})")
    print(f"Live Trading: {TRADING.live_trading}")
    print(f"API Key Set: {'Yes' if BINANCE.api_key else 'NO'}")
    print(f"API Secret Set: {'Yes' if BINANCE.api_secret else 'NO'}")
    print("=" * 40)
