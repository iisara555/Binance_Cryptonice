"""
Single source of truth for THB_* / legacy internal symbols → Binance.th spot pairs.

Built from ``coin_whitelist.json`` (via :class:`dynamic_coin_config.JsonCoinWhitelistRepository`)
merged with legacy aliases (e.g. THB_MATIC → POLUSDT).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

from dynamic_coin_config import (
    HybridDynamicCoinConfig,
    JsonCoinWhitelistRepository,
    _build_pair,
    resolve_whitelist_path,
)
from project_paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_WHITELIST_JSON = "coin_whitelist.json"

# Optional override (e.g. set from ``main`` using ``data.hybrid_dynamic_coin_config.whitelist_json_path``).
_ACTIVE_WHITELIST_JSON: Optional[str] = None


def set_whitelist_json_path(rel: Optional[str]) -> None:
    """Point the registry at a project-relative whitelist file and invalidate cache."""
    global _ACTIVE_WHITELIST_JSON
    _ACTIVE_WHITELIST_JSON = (str(rel).strip() if rel else None) or None
    clear_symbol_map_cache()


# Baseline map when JSON is missing or incomplete; whitelist entries override by THB_{ASSET}.
LEGACY_THB_SYMBOL_MAP: Dict[str, str] = {
    "THB_BTC": "BTCUSDT",
    "THB_ETH": "ETHUSDT",
    "THB_BNB": "BNBUSDT",
    "THB_DOGE": "DOGEUSDT",
    "THB_XRP": "XRPUSDT",
    "THB_SOL": "SOLUSDT",
    "THB_ADA": "ADAUSDT",
    "THB_DOT": "DOTUSDT",
    "THB_LINK": "LINKUSDT",
    "THB_MATIC": "POLUSDT",
    "THB_POL": "POLUSDT",
}

_map_cache: Dict[str, object] = {"key": None, "map": None}


def clear_symbol_map_cache() -> None:
    """Reset cached map (tests / hot-reload tooling)."""
    _map_cache["key"] = None
    _map_cache["map"] = None


def build_symbol_map_from_hybrid(cfg: HybridDynamicCoinConfig) -> Dict[str, str]:
    out = dict(LEGACY_THB_SYMBOL_MAP)
    quote = cfg.quote_asset
    for entry in cfg.entries:
        if not entry.enabled:
            continue
        base = str(entry.symbol or "").strip().upper()
        if not base:
            continue
        pair = _build_pair(base, quote)
        if not pair:
            continue
        out[f"THB_{base}"] = pair
    return out


def get_symbol_map(
    project_root: Optional[Path] = None,
    *,
    whitelist_json_path: Optional[str] = None,
) -> Dict[str, str]:
    """Return merged THB_* → Binance symbol map; refreshes when whitelist file mtime changes."""
    root = project_root or PROJECT_ROOT
    rel = whitelist_json_path or _ACTIVE_WHITELIST_JSON or DEFAULT_WHITELIST_JSON
    path = resolve_whitelist_path(rel, root)
    try:
        mtime = float(path.stat().st_mtime)
    except OSError:
        mtime = -1.0
    cache_key = (str(path.resolve()), mtime)
    if _map_cache.get("key") == cache_key and isinstance(_map_cache.get("map"), dict):
        return _map_cache["map"]  # type: ignore[return-value]

    repo = JsonCoinWhitelistRepository(default_path=path)
    cfg = repo.load(path)
    new_map = build_symbol_map_from_hybrid(cfg)
    _map_cache["key"] = cache_key
    _map_cache["map"] = new_map
    logger.debug("Symbol map loaded from %s (%d entries)", path, len(new_map))
    return new_map
