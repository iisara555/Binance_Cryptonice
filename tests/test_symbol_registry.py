"""Tests for symbol_registry (no exchange HTTP)."""

import unittest

from dynamic_coin_config import CoinWhitelistEntry, HybridDynamicCoinConfig
from symbol_registry import build_symbol_map_from_hybrid, clear_symbol_map_cache, get_symbol_map


def _cfg(entries, **kwargs):
    return HybridDynamicCoinConfig(
        version=1,
        quote_asset="USDT",
        min_quote_balance_usdt=100.0,
        min_quote_balance_for_pairs=None,
        require_supported_market=True,
        include_assets_with_balance=True,
        entries=entries,
        source_path="test",
        source_kind="test",
        warnings=[],
        **kwargs,
    )


class SymbolRegistryTests(unittest.TestCase):
    def test_build_symbol_map_includes_whitelist_assets(self):
        cfg = _cfg([CoinWhitelistEntry(symbol="DOT", enabled=True)])
        m = build_symbol_map_from_hybrid(cfg)
        self.assertEqual(m["THB_DOT"], "DOTUSDT")
        self.assertEqual(m["THB_BTC"], "BTCUSDT")

    def test_legacy_matic_maps_to_pol(self):
        cfg = _cfg([CoinWhitelistEntry(symbol="POL", enabled=True)])
        m = build_symbol_map_from_hybrid(cfg)
        self.assertEqual(m["THB_MATIC"], "POLUSDT")
        self.assertEqual(m["THB_POL"], "POLUSDT")

    def test_get_symbol_map_cache_refresh(self):
        clear_symbol_map_cache()
        a = get_symbol_map()
        b = get_symbol_map()
        self.assertEqual(a, b)
        self.assertIn("THB_BTC", a)


if __name__ == "__main__":
    unittest.main()
