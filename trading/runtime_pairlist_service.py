"""Runtime pairlist whitelist / refresh behavior delegated from ``TradingBotApp``."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List

from dynamic_coin_config import resolve_whitelist_path
from project_paths import PROJECT_ROOT
from trading.bootstrap_config import get_hybrid_dynamic_coin_settings as _get_hybrid_dynamic_coin_settings
from trading.cli_pair_normalize import extract_asset_from_pair as _extract_asset_from_pair
from trading.cli_pair_normalize import normalize_cli_pair as _normalize_cli_pair
from trading.cli_pair_normalize import normalize_pairs as _normalize_pairs

logger = logging.getLogger(__name__)


class RuntimePairlistService:
    __slots__ = ("_app",)

    def __init__(self, app: Any) -> None:
        self._app = app

    def load_runtime_pairlist_document(self) -> tuple[Path, Dict[str, Any], List[str]]:
        app = self._app
        data_config = app.config.setdefault("data", {})
        settings = _get_hybrid_dynamic_coin_settings(data_config)
        whitelist_path = resolve_whitelist_path(settings.get("whitelist_json_path"), PROJECT_ROOT)

        raw_document: Dict[str, Any] = {}
        if whitelist_path.exists():
            try:
                loaded = json.loads(whitelist_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    raw_document = loaded
            except Exception as exc:
                logger.warning("Failed to parse runtime pairlist %s: %s", whitelist_path, exc)

        raw_entries = raw_document.get("assets")
        if raw_entries is None:
            raw_entries = raw_document.get("whitelist")
        if raw_entries is None:
            raw_entries = raw_document.get("pairs")

        normalized_assets: List[str] = []
        seen_assets: set[str] = set()
        for entry in raw_entries or []:
            enabled = True
            if isinstance(entry, dict):
                enabled = bool(entry.get("enabled", True))
                entry = entry.get("symbol") or entry.get("asset") or entry.get("pair")
            asset = _extract_asset_from_pair(entry)
            if not asset or asset in {"THB", "USDT"} or not enabled or asset in seen_assets:
                continue
            seen_assets.add(asset)
            normalized_assets.append(asset)

        raw_document.setdefault("version", 1)
        raw_document.setdefault("quote_asset", "USDT")
        raw_document.setdefault("min_quote_balance_thb", settings.get("min_quote_balance_thb", 100.0))
        raw_document.setdefault("require_supported_market", settings.get("require_supported_market", True))
        raw_document.setdefault("include_assets_with_balance", settings.get("include_assets_with_balance", True))
        return whitelist_path, raw_document, normalized_assets

    def write_runtime_pairlist_document(self, path: Path, document: Dict[str, Any], assets: List[str]) -> None:
        updated = dict(document)
        updated["assets"] = list(assets)
        updated.pop("whitelist", None)
        updated.pop("pairs", None)
        path.write_text(json.dumps(updated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def add_runtime_pairs(self, pairs: Iterable[str]) -> Dict[str, Any]:
        app = self._app
        normalized_pairs = _normalize_pairs(_normalize_cli_pair(pair) for pair in (pairs or []))
        if not normalized_pairs:
            raise ValueError("At least one pair is required")

        whitelist_path, document, current_assets = self.load_runtime_pairlist_document()
        updated_assets = list(current_assets)
        added_pairs: List[str] = []
        for pair in normalized_pairs:
            asset = _extract_asset_from_pair(pair)
            if asset not in updated_assets:
                updated_assets.append(asset)
                added_pairs.append(pair)

        if added_pairs:
            self.write_runtime_pairlist_document(whitelist_path, document, updated_assets)

        if app.config.setdefault("data", {}).get("auto_detect_held_pairs", True):
            active_pairs = app.refresh_runtime_pairs(reason="cli pair add", force=True)
        else:
            current_runtime_pairs = app.config.setdefault("data", {}).get("pairs") or []
            active_pairs = app._apply_runtime_pairs_update(
                _normalize_pairs(list(current_runtime_pairs) + normalized_pairs),
                reason="cli pair add",
                force=True,
            )

        return {
            "status": "ok",
            "added_pairs": added_pairs,
            "pairlist_path": str(whitelist_path),
            "active_pairs": active_pairs,
        }

    def remove_runtime_pairs(self, pairs: Iterable[str]) -> Dict[str, Any]:
        app = self._app
        normalized_pairs = _normalize_pairs(_normalize_cli_pair(pair) for pair in (pairs or []))
        if not normalized_pairs:
            raise ValueError("At least one pair is required")

        whitelist_path, document, current_assets = self.load_runtime_pairlist_document()
        remove_assets = {_extract_asset_from_pair(pair) for pair in normalized_pairs}
        updated_assets = [asset for asset in current_assets if asset not in remove_assets]
        quote_asset = str(document.get("quote_asset") or "USDT").upper()
        removed_pairs = [
            f"{asset}{quote_asset}" if quote_asset == "USDT" else f"{quote_asset}_{asset}"
            for asset in current_assets
            if asset in remove_assets
        ]
        self.write_runtime_pairlist_document(whitelist_path, document, updated_assets)

        if app.config.setdefault("data", {}).get("auto_detect_held_pairs", True):
            active_pairs = app.refresh_runtime_pairs(reason="cli pair remove", force=True)
        else:
            current_runtime_pairs = [
                pair
                for pair in (app.config.setdefault("data", {}).get("pairs") or [])
                if _extract_asset_from_pair(pair) not in remove_assets
            ]
            active_pairs = app._apply_runtime_pairs_update(current_runtime_pairs, reason="cli pair remove", force=True)

        return {
            "status": "ok",
            "removed_pairs": removed_pairs,
            "pairlist_path": str(whitelist_path),
            "active_pairs": active_pairs,
        }

    def get_runtime_pairlist_status(self) -> Dict[str, Any]:
        app = self._app
        whitelist_path, document, configured_assets = self.load_runtime_pairlist_document()
        quote_asset = str(document.get("quote_asset") or "USDT").upper()
        return {
            "status": "ok",
            "pairlist_path": str(whitelist_path),
            "configured_pairs": [
                f"{asset}{quote_asset}" if quote_asset == "USDT" else f"{quote_asset}_{asset}"
                for asset in configured_assets
            ],
            "active_pairs": list(app.config.setdefault("data", {}).get("pairs") or []),
        }
