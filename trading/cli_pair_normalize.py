"""CLI-facing pair/string normalization extracted from ``main.TradingBotApp`` callers."""

from __future__ import annotations

import re
from typing import Any, Iterable, List

ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


def normalize_pairs(pairs: Iterable[str]) -> List[str]:
    """Normalize pair strings and drop blanks while preserving order."""
    normalized: List[str] = []
    seen: set[str] = set()
    for pair in pairs or []:
        value = str(pair or "").strip().upper()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def sanitize_cli_input_line(value: Any) -> str:
    """Strip ANSI escape sequences from line-buffered CLI input."""
    text = str(value or "")
    sanitized = ANSI_ESCAPE_RE.sub("", text)
    return sanitized.replace("\x1b", "")


def normalize_cli_pair(value: Any) -> str:
    """Normalize user-facing pair inputs like BTC, BTCUSDT, THB_BTC, or BTC_THB."""
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    for quote in ("USDT", "THB"):
        if raw.startswith(f"{quote}_"):
            return f"{raw.split('_', 1)[1]}USDT"
        if raw.endswith(f"_{quote}"):
            return f"{raw.rsplit('_', 1)[0]}USDT"
    if raw.endswith("USDT") and "_" not in raw:
        return raw
    if "_" not in raw:
        return f"{raw}USDT"
    return raw


def extract_asset_from_pair(value: Any) -> str:
    normalized = normalize_cli_pair(value)
    if normalized.endswith("USDT") and "_" not in normalized:
        return normalized[:-4]
    if normalized.startswith("THB_") or normalized.startswith("USDT_"):
        return normalized.split("_", 1)[1]
    if normalized.endswith("_THB") or normalized.endswith("_USDT"):
        return normalized.rsplit("_", 1)[0]
    return normalized
