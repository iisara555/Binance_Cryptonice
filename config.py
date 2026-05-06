"""
Configuration for Binance Thailand Crypto Trading Bot.
Loads settings from environment variables (.env file).

SECURITY: All sensitive credentials must be loaded from environment variables.
          This module will CRITICALLY FAIL on startup if required env vars are missing.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from project_paths import PROJECT_ROOT

# Load .env via python-dotenv first so BaseSettings reads the populated os.environ
try:
    from dotenv import load_dotenv

    _env_paths = [PROJECT_ROOT / ".env", Path.cwd() / ".env"]
    for _env_path in _env_paths:
        if _env_path.exists():
            load_dotenv(_env_path, override=True)
            break
    else:
        import os as _os
        if not _os.environ.get("BINANCE_API_KEY"):
            print("[WARNING] No .env file found and BINANCE_API_KEY not set")
except ImportError:
    pass  # python-dotenv not installed, rely on system env vars


# ── Paths / constants ─────────────────────────────────────────────────────────

ENV_FILE: Path = PROJECT_ROOT / ".env"

# Primary fitness metric for all trading decisions and future ML tuning.
SYSTEM_OBJECTIVE: str = "MAXIMIZE_NET_PROFIT"

# Minimum Risk:Reward ratio to approve a trade.
MIN_RISK_REWARD_RATIO: float = 1.1  # ADJUSTED v2.1 - loosened for signal generation

_PLACEHOLDERS = ("your_", "replace_this", "placeholder", "changeme")


# ── Settings models ───────────────────────────────────────────────────────────


class BinanceConfig(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    api_key: str = Field(validation_alias="BINANCE_API_KEY")
    api_secret: str = Field(validation_alias="BINANCE_API_SECRET")
    base_url: str = Field(default="https://api.binance.th", validation_alias="BINANCE_BASE_URL")
    default_symbol: str = Field(default="BTCUSDT", validation_alias="DEFAULT_SYMBOL")

    @field_validator("api_key", "api_secret", mode="before")
    @classmethod
    def _no_placeholder(cls, v: object) -> str:
        s = str(v or "").strip()
        if not s:
            raise ValueError("must not be empty")
        for pattern in _PLACEHOLDERS:
            if pattern in s.lower():
                raise ValueError(f"appears to be a placeholder value ('{s}')")
        return s


class TradingConfig(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    min_order_amount: float = Field(default=10.0, validation_alias="MIN_ORDER_AMOUNT", gt=0)  # Aligned with bot_config.yaml trading.min_order_amount
    default_order_type: str = Field(default="limit", validation_alias="DEFAULT_ORDER_TYPE")
    live_trading: bool = Field(default=False, validation_alias="LIVE_TRADING")


class BotConfig(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")


# ── Config Instances ──────────────────────────────────────────────────────────

try:
    BINANCE = BinanceConfig()
except ValidationError as exc:
    print("\n" + "=" * 60)
    print("FATAL: Binance API credentials not configured!")
    print("=" * 60)
    for err in exc.errors():
        loc = ".".join(str(l) for l in err.get("loc", ("?",)))
        print(f"  {loc}: {err['msg']}")
    print("\nTo fix this:")
    print("1. Create a .env file in the project root")
    print("2. Add the following lines:")
    print("   BINANCE_API_KEY=your_actual_api_key")
    print("   BINANCE_API_SECRET=your_actual_api_secret")
    print("\nGet your API keys from: https://www.binance.th")
    print("=" * 60 + "\n")
    sys.exit(1)

TRADING = TradingConfig()
BOT = BotConfig()


# ── Validation ────────────────────────────────────────────────────────────────


def validate_config() -> tuple[list[str], list[str]]:
    """Return (critical_errors, warnings). Empty critical = all good."""
    import os

    critical: list[str] = []
    warnings: list[str] = []

    if TRADING.live_trading:
        warnings.append("⚠️ LIVE_TRADING is enabled — real orders WILL be placed!")

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


def enforce_critical_config() -> None:
    """Enforce that critical configuration is present at bot startup."""
    critical, warnings = validate_config()
    for warning in warnings:
        print(f"[WARNING] {warning}")
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
    print(f"Min Order Amount: {TRADING.min_order_amount}")
    print("=" * 40)
