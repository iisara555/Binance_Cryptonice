"""Hybrid dynamic coin configuration for runtime trading pair selection."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_WHITELIST_JSON = "coin_whitelist.json"
DEFAULT_QUOTE_ASSET = "THB"
DEFAULT_MIN_QUOTE_BALANCE_THB = 100.0
DEFAULT_WHITELIST_ASSETS = ("BTC", "DOGE")
SUPPORTED_WHITELIST_SCHEMA_VERSION = 1
TOP_LEVEL_SCHEMA_KEYS = {
    "version",
    "quote_asset",
    "min_quote_balance_thb",
    "require_supported_market",
    "include_assets_with_balance",
    "assets",
    "whitelist",
    "pairs",
}
ENTRY_SCHEMA_KEYS = {
    "symbol",
    "asset",
    "pair",
    "enabled",
    "min_asset_balance",
    "min_quote_balance_thb",
    "include_if_held",
    "require_supported_market",
}


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    return default


def _normalize_asset(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    if raw.startswith(f"{DEFAULT_QUOTE_ASSET}_"):
        return raw.split("_", 1)[1]
    if raw.endswith(f"_{DEFAULT_QUOTE_ASSET}"):
        return raw.rsplit("_", 1)[0]
    return raw


def _normalize_pairs(pairs: Iterable[str]) -> List[str]:
    normalized: List[str] = []
    seen: set[str] = set()
    for pair in pairs or []:
        value = str(pair or "").strip().upper()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _build_pair(asset: str, quote_asset: str = DEFAULT_QUOTE_ASSET) -> str:
    return f"{quote_asset.upper()}_{_normalize_asset(asset)}"


def _extract_supported_thb_pairs(symbol_rows: Iterable[Dict[str, Any]]) -> set[str]:
    pairs: set[str] = set()
    for row in symbol_rows or []:
        raw_symbol = str((row or {}).get("symbol") or "").upper()
        if raw_symbol.endswith(f"_{DEFAULT_QUOTE_ASSET}"):
            asset = raw_symbol[: -(len(DEFAULT_QUOTE_ASSET) + 1)]
            if asset:
                pairs.add(_build_pair(asset))
        elif raw_symbol.startswith(f"{DEFAULT_QUOTE_ASSET}_"):
            pairs.add(raw_symbol)
    return pairs


def resolve_whitelist_path(path_value: Optional[str], project_root: Optional[Path]) -> Path:
    candidate = Path(path_value) if path_value else Path(DEFAULT_WHITELIST_JSON)
    if candidate.is_absolute():
        return candidate
    if project_root:
        return Path(project_root) / candidate
    return candidate


@dataclass(frozen=True)
class CoinWhitelistEntry:
    symbol: str
    enabled: bool = True
    min_asset_balance: float = 0.0
    min_quote_balance_thb: Optional[float] = None
    include_if_held: Optional[bool] = None
    require_supported_market: Optional[bool] = None


@dataclass(frozen=True)
class HybridDynamicCoinConfig:
    version: int
    quote_asset: str
    min_quote_balance_thb: float
    require_supported_market: bool
    include_assets_with_balance: bool
    entries: List[CoinWhitelistEntry]
    source_path: str
    source_kind: str
    warnings: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class RuntimePairSelection:
    pairs: List[str]
    quote_balance_thb: float
    warnings: List[str]
    source_kind: str
    source_path: str


class JsonCoinWhitelistRepository:
    """Repository that loads the whitelist from an external JSON file."""

    def __init__(self, default_path: Path):
        self.default_path = Path(default_path)

    def load(self, path: Optional[Path] = None) -> HybridDynamicCoinConfig:
        target = Path(path or self.default_path)
        if not target.exists():
            logger.warning(
                "Hybrid coin whitelist JSON not found at %s - using safe defaults %s",
                target,
                list(DEFAULT_WHITELIST_ASSETS),
            )
            return self._default_config(target, "missing_file")

        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "Failed to parse hybrid coin whitelist JSON %s: %s - using safe defaults",
                target,
                exc,
            )
            return self._default_config(target, "invalid_json")

        if not isinstance(raw, dict):
            logger.warning(
                "Hybrid coin whitelist JSON %s must contain an object root - using safe defaults",
                target,
            )
            return self._default_config(target, "invalid_shape")

        schema_version = raw.get("version", SUPPORTED_WHITELIST_SCHEMA_VERSION)
        try:
            schema_version = int(schema_version)
        except (TypeError, ValueError):
            logger.warning(
                "Hybrid coin whitelist JSON %s has invalid schema version %r - using safe defaults",
                target,
                raw.get("version"),
            )
            return self._default_config(target, "invalid_version")

        if schema_version != SUPPORTED_WHITELIST_SCHEMA_VERSION:
            logger.warning(
                "Hybrid coin whitelist JSON %s uses unsupported schema version %s - using safe defaults",
                target,
                schema_version,
            )
            return self._default_config(target, "unsupported_version")

        warnings = self._collect_unknown_keys(raw, TOP_LEVEL_SCHEMA_KEYS, "root")

        entries, entry_warnings = self._parse_entries(raw)
        warnings.extend(entry_warnings)
        if not entries:
            logger.warning(
                "Hybrid coin whitelist JSON %s contains no valid enabled assets - using safe defaults",
                target,
            )
            return self._default_config(target, "empty_entries")

        for warning in warnings:
            logger.warning("Hybrid coin whitelist schema warning: %s", warning)

        return HybridDynamicCoinConfig(
            version=schema_version,
            quote_asset=str(raw.get("quote_asset") or DEFAULT_QUOTE_ASSET).upper(),
            min_quote_balance_thb=max(
                0.0,
                _coerce_float(raw.get("min_quote_balance_thb"), DEFAULT_MIN_QUOTE_BALANCE_THB),
            ),
            require_supported_market=_coerce_bool(raw.get("require_supported_market"), True),
            include_assets_with_balance=_coerce_bool(raw.get("include_assets_with_balance"), True),
            entries=entries,
            source_path=str(target),
            source_kind="json",
            warnings=warnings,
        )

    def list_candidate_pairs(self, path: Optional[Path] = None) -> List[str]:
        config = self.load(path)
        return [_build_pair(entry.symbol, config.quote_asset) for entry in config.entries if entry.enabled]

    def _parse_entries(self, raw: Dict[str, Any]) -> tuple[List[CoinWhitelistEntry], List[str]]:
        raw_entries = raw.get("assets")
        if raw_entries is None:
            raw_entries = raw.get("whitelist")
        if raw_entries is None:
            raw_entries = raw.get("pairs")

        parsed: List[CoinWhitelistEntry] = []
        warnings: List[str] = []
        seen: set[str] = set()
        for index, item in enumerate(raw_entries or []):
            entry, entry_warnings = self._parse_entry(item, index)
            warnings.extend(entry_warnings)
            if not entry or not entry.enabled or entry.symbol in seen:
                continue
            seen.add(entry.symbol)
            parsed.append(entry)
        return parsed, warnings

    def _parse_entry(self, raw_entry: Any, index: int) -> tuple[Optional[CoinWhitelistEntry], List[str]]:
        warnings: List[str] = []
        if isinstance(raw_entry, str):
            symbol = _normalize_asset(raw_entry)
            return (CoinWhitelistEntry(symbol=symbol) if symbol else None, warnings)

        if not isinstance(raw_entry, dict):
            warnings.append(f"assets[{index}] ignored: expected object or string, got {type(raw_entry).__name__}")
            return None, warnings

        warnings.extend(self._collect_unknown_keys(raw_entry, ENTRY_SCHEMA_KEYS, f"assets[{index}]") )

        symbol = _normalize_asset(
            raw_entry.get("symbol")
            or raw_entry.get("asset")
            or raw_entry.get("pair")
        )
        if not symbol or symbol == DEFAULT_QUOTE_ASSET:
            warnings.append(f"assets[{index}] ignored: symbol is missing or invalid")
            return None, warnings

        return CoinWhitelistEntry(
            symbol=symbol,
            enabled=_coerce_bool(raw_entry.get("enabled"), True),
            min_asset_balance=max(0.0, _coerce_float(raw_entry.get("min_asset_balance"), 0.0)),
            min_quote_balance_thb=(
                max(0.0, _coerce_float(raw_entry.get("min_quote_balance_thb"), 0.0))
                if raw_entry.get("min_quote_balance_thb") is not None
                else None
            ),
            include_if_held=(
                _coerce_bool(raw_entry.get("include_if_held"), True)
                if raw_entry.get("include_if_held") is not None
                else None
            ),
            require_supported_market=(
                _coerce_bool(raw_entry.get("require_supported_market"), True)
                if raw_entry.get("require_supported_market") is not None
                else None
            ),
        ), warnings

    def _collect_unknown_keys(self, raw: Dict[str, Any], allowed_keys: set[str], scope: str) -> List[str]:
        return [
            f"{scope}: unknown key '{key}'"
            for key in sorted(set(raw.keys()) - allowed_keys)
        ]

    def _default_config(self, path: Path, source_kind: str) -> HybridDynamicCoinConfig:
        return HybridDynamicCoinConfig(
            version=SUPPORTED_WHITELIST_SCHEMA_VERSION,
            quote_asset=DEFAULT_QUOTE_ASSET,
            min_quote_balance_thb=DEFAULT_MIN_QUOTE_BALANCE_THB,
            require_supported_market=True,
            include_assets_with_balance=True,
            entries=[CoinWhitelistEntry(symbol=symbol) for symbol in DEFAULT_WHITELIST_ASSETS],
            source_path=str(path),
            source_kind=source_kind,
            warnings=[],
        )


class HybridDynamicPairResolver:
    """Facade that combines JSON whitelist config with live exchange readiness checks."""

    def __init__(self, repository: JsonCoinWhitelistRepository):
        self.repository = repository

    def resolve(
        self,
        api_client,
        *,
        config_path: Optional[Path] = None,
        configured_pairs: Optional[Iterable[str]] = None,
        min_quote_balance_thb: Optional[float] = None,
        require_supported_market: Optional[bool] = None,
        include_assets_with_balance: Optional[bool] = None,
    ) -> RuntimePairSelection:
        config = self.repository.load(config_path)
        if min_quote_balance_thb is not None:
            config = replace(config, min_quote_balance_thb=max(0.0, float(min_quote_balance_thb)))
        if require_supported_market is not None:
            config = replace(config, require_supported_market=bool(require_supported_market))
        if include_assets_with_balance is not None:
            config = replace(config, include_assets_with_balance=bool(include_assets_with_balance))

        supported_pairs: set[str] = set()
        warnings: List[str] = list(config.warnings)
        try:
            supported_pairs = _extract_supported_thb_pairs(api_client.get_symbols() or [])
        except Exception as exc:
            warnings.append(f"Failed to fetch Bitkub symbols: {exc}")

        balances = api_client.get_balances() or {}
        quote_balance = self._extract_available_balance(balances, config.quote_asset)
        configured_allow_list = set(_normalize_pairs(configured_pairs or []))

        selected_pairs: List[str] = []
        for entry in config.entries:
            pair = _build_pair(entry.symbol, config.quote_asset)

            if configured_allow_list and pair not in configured_allow_list:
                continue

            require_supported_market = (
                entry.require_supported_market
                if entry.require_supported_market is not None
                else config.require_supported_market
            )
            if require_supported_market and supported_pairs and pair not in supported_pairs:
                warnings.append(f"{pair} skipped: pair is not supported by Bitkub")
                continue

            if self._is_trade_ready(entry, balances, quote_balance, config):
                selected_pairs.append(pair)

        return RuntimePairSelection(
            pairs=selected_pairs,
            quote_balance_thb=quote_balance,
            warnings=warnings,
            source_kind=config.source_kind,
            source_path=config.source_path,
        )

    def list_candidate_pairs(self, config_path: Optional[Path] = None) -> List[str]:
        return self.repository.list_candidate_pairs(config_path)

    def _is_trade_ready(
        self,
        entry: CoinWhitelistEntry,
        balances: Dict[str, Any],
        quote_balance: float,
        config: HybridDynamicCoinConfig,
    ) -> bool:
        required_quote = (
            entry.min_quote_balance_thb
            if entry.min_quote_balance_thb is not None
            else config.min_quote_balance_thb
        )
        asset_balance = self._extract_total_balance(balances, entry.symbol)
        include_if_held = (
            entry.include_if_held
            if entry.include_if_held is not None
            else config.include_assets_with_balance
        )
        has_asset_balance = include_if_held and asset_balance > entry.min_asset_balance
        has_quote_balance = quote_balance >= required_quote if required_quote > 0 else quote_balance > 0
        return has_asset_balance or has_quote_balance

    def _extract_available_balance(self, balances: Dict[str, Any], asset: str) -> float:
        raw = balances.get(asset.upper(), {}) if isinstance(balances, dict) else {}
        if isinstance(raw, dict):
            return _coerce_float(raw.get("available"), 0.0)
        return _coerce_float(raw, 0.0)

    def _extract_total_balance(self, balances: Dict[str, Any], asset: str) -> float:
        raw = balances.get(asset.upper(), {}) if isinstance(balances, dict) else {}
        if isinstance(raw, dict):
            return _coerce_float(raw.get("available"), 0.0) + _coerce_float(raw.get("reserved"), 0.0)
        return _coerce_float(raw, 0.0)