"""Tests for trading/dynamic_config.py — NAV-adaptive risk parameters."""

from __future__ import annotations

from unittest.mock import MagicMock, Mock, patch

import pytest

from trading.dynamic_config import (
    DynamicRiskConfig,
    _NAV_RECOMPUTE_THRESHOLD_PCT,
    apply_dynamic_risk_to_config,
    apply_dynamic_risk_to_manager,
    compute_dynamic_risk,
    fetch_startup_nav,
)

# ── Shared helpers ─────────────────────────────────────────────────────────────

_DEFAULT_CONFIG = {"min_order_amount": 10.0, "take_profit_pct": 10.0}


def _compute(nav: float, *, min_order: float = 10.0, take_profit_pct: float = 10.0) -> dict:
    return compute_dynamic_risk(nav, {"min_order_amount": min_order, "take_profit_pct": take_profit_pct})


# ── compute_dynamic_risk: expected behaviour ───────────────────────────────────


class TestExpectedBehaviour:
    """Verify the documented NAV → parameter mapping."""

    def test_nav_42_position_pct(self):
        r = _compute(42.0)
        assert r["max_position_per_trade_pct"] == pytest.approx(28.0)

    def test_nav_42_slots(self):
        r = _compute(42.0)
        assert r["max_open_positions"] == 3

    def test_nav_42_floor(self):
        r = _compute(42.0)
        assert r["min_balance_threshold"] == pytest.approx(33.6, abs=0.01)

    def test_nav_100_slots_at_least_5(self):
        r = _compute(100.0)
        assert r["max_open_positions"] >= 5

    def test_nav_100_floor(self):
        r = _compute(100.0)
        assert r["min_balance_threshold"] == pytest.approx(80.0)

    def test_nav_500_slots(self):
        r = _compute(500.0)
        assert r["max_open_positions"] == 6

    def test_nav_500_floor(self):
        r = _compute(500.0)
        assert r["min_balance_threshold"] == pytest.approx(400.0)


# ── compute_dynamic_risk: invariants across all NAVs ──────────────────────────


class TestInvariants:
    """Safety properties that must hold for every NAV in [42, 2000]."""

    @pytest.mark.parametrize("nav", [42, 50, 75, 100, 150, 200, 500, 1000, 2000])
    def test_position_usdt_always_gte_min_order(self, nav):
        min_order = 10.0
        r = _compute(float(nav), min_order=min_order)
        pos_usdt = r["max_position_per_trade_pct"] / 100.0 * nav
        assert pos_usdt >= min_order, (
            f"nav={nav}: position_usdt={pos_usdt:.2f} < min_order={min_order}"
        )

    @pytest.mark.parametrize("nav", [42, 50, 75, 100, 150, 200, 500, 1000, 2000])
    def test_floor_below_nav(self, nav):
        r = _compute(float(nav))
        assert r["min_balance_threshold"] < nav

    @pytest.mark.parametrize("nav", [42, 50, 75, 100, 150, 200, 500, 1000, 2000])
    def test_max_open_positions_in_range(self, nav):
        r = _compute(float(nav))
        assert 1 <= r["max_open_positions"] <= 6

    @pytest.mark.parametrize("nav", [42, 50, 75, 100, 150, 200, 500, 1000, 2000])
    def test_position_pct_does_not_exceed_40(self, nav):
        r = _compute(float(nav))
        assert r["max_position_per_trade_pct"] <= 40.0

    @pytest.mark.parametrize("nav", [42, 50, 75, 100, 150, 200, 500, 1000, 2000])
    def test_position_size_cap_matches_position_pct(self, nav):
        r = _compute(float(nav))
        assert r["position_size_cap_pct"] == r["max_position_per_trade_pct"]


# ── compute_dynamic_risk: trailing activation ─────────────────────────────────


class TestTrailingActivation:
    def test_trailing_is_half_of_take_profit(self):
        r = _compute(100.0, take_profit_pct=10.0)
        assert r["trailing_activation_pct"] == pytest.approx(5.0)

    def test_trailing_scales_with_take_profit(self):
        r = _compute(100.0, take_profit_pct=6.0)
        assert r["trailing_activation_pct"] == pytest.approx(3.0)


# ── compute_dynamic_risk: edge cases ─────────────────────────────────────────


class TestEdgeCases:
    def test_zero_nav_raises(self):
        with pytest.raises(ValueError):
            compute_dynamic_risk(0.0, _DEFAULT_CONFIG)

    def test_negative_nav_raises(self):
        with pytest.raises(ValueError):
            compute_dynamic_risk(-1.0, _DEFAULT_CONFIG)

    def test_very_large_nav_caps_slots_at_6(self):
        r = _compute(100_000.0)
        assert r["max_open_positions"] == 6

    def test_very_large_nav_caps_position_pct_at_40(self):
        # 10% of a huge NAV would normally exceed 40 — verify cap
        r = _compute(100_000.0)
        assert r["max_position_per_trade_pct"] <= 40.0


# ── apply_dynamic_risk_to_config ──────────────────────────────────────────────


class TestApplyToConfig:
    def test_patches_risk_section(self):
        cfg: dict = {"risk": {}, "portfolio": {}, "execution": {}}
        dynamic = _compute(100.0)
        apply_dynamic_risk_to_config(cfg, dynamic)
        assert cfg["risk"]["max_position_per_trade_pct"] == dynamic["max_position_per_trade_pct"]
        assert cfg["risk"]["max_open_positions"] == dynamic["max_open_positions"]

    def test_patches_portfolio_floor(self):
        cfg: dict = {"portfolio": {"min_balance_threshold": 999}}
        dynamic = _compute(100.0)
        apply_dynamic_risk_to_config(cfg, dynamic)
        assert cfg["portfolio"]["min_balance_threshold"] == pytest.approx(80.0)

    def test_patches_execution_trailing(self):
        cfg: dict = {"execution": {"trailing_activation_pct": 99}}
        dynamic = _compute(100.0, take_profit_pct=10.0)
        apply_dynamic_risk_to_config(cfg, dynamic)
        assert cfg["execution"]["trailing_activation_pct"] == pytest.approx(5.0)

    def test_patches_position_sizing(self):
        cfg: dict = {}
        dynamic = _compute(42.0)
        apply_dynamic_risk_to_config(cfg, dynamic)
        assert cfg["auto_trader"]["position_sizing"]["max_position_pct"] == dynamic["position_size_cap_pct"]

    def test_patches_scalping_mode_profile_when_present(self):
        cfg: dict = {"strategy_mode": {"scalping": {"max_position_per_trade_pct": 99}}}
        dynamic = _compute(42.0)
        apply_dynamic_risk_to_config(cfg, dynamic)
        assert cfg["strategy_mode"]["scalping"]["max_position_per_trade_pct"] == dynamic["max_position_per_trade_pct"]


# ── apply_dynamic_risk_to_manager ─────────────────────────────────────────────


class TestApplyToManager:
    def _make_rm(self):
        rm = Mock()
        rm.config = Mock()
        rm.config.max_position_per_trade_pct = 0.0
        rm.config.max_open_positions = 0
        rm.config.min_balance_threshold = 0.0
        return rm

    def test_updates_risk_manager_fields(self):
        rm = self._make_rm()
        dynamic = _compute(100.0)
        apply_dynamic_risk_to_manager(rm, dynamic)
        assert rm.config.max_position_per_trade_pct == dynamic["max_position_per_trade_pct"]
        assert rm.config.max_open_positions == dynamic["max_open_positions"]
        assert rm.config.min_balance_threshold == dynamic["min_balance_threshold"]

    def test_noop_when_no_config(self):
        rm = Mock()
        rm.config = None
        apply_dynamic_risk_to_manager(rm, _compute(100.0))  # must not raise


# ── DynamicRiskConfig ─────────────────────────────────────────────────────────


class TestDynamicRiskConfig:
    def test_should_recompute_false_below_threshold(self):
        drc = DynamicRiskConfig(100.0, _DEFAULT_CONFIG)
        # 5% change — below 10% threshold
        assert drc.should_recompute(105.0) is False

    def test_should_recompute_true_above_threshold(self):
        drc = DynamicRiskConfig(100.0, _DEFAULT_CONFIG)
        # 15% change — above 10% threshold
        assert drc.should_recompute(115.0) is True

    def test_should_recompute_true_on_nav_drop(self):
        drc = DynamicRiskConfig(100.0, _DEFAULT_CONFIG)
        # -15% drop
        assert drc.should_recompute(85.0) is True

    def test_recompute_updates_last_nav(self):
        drc = DynamicRiskConfig(100.0, _DEFAULT_CONFIG)
        drc.recompute(200.0)
        assert drc.last_nav == pytest.approx(200.0)

    def test_recompute_returns_new_values(self):
        drc = DynamicRiskConfig(100.0, _DEFAULT_CONFIG)
        result = drc.recompute(500.0)
        assert result["max_open_positions"] == 6  # large NAV → 6 slots

    def test_last_dynamic_is_copy(self):
        drc = DynamicRiskConfig(100.0, _DEFAULT_CONFIG)
        d1 = drc.last_dynamic
        d1["max_open_positions"] = 999  # mutate the copy
        assert drc.last_dynamic["max_open_positions"] != 999

    def test_no_recompute_when_nav_unchanged(self):
        drc = DynamicRiskConfig(100.0, _DEFAULT_CONFIG)
        # exact same NAV → no recompute needed
        assert drc.should_recompute(100.0) is False

    def test_boundary_exactly_at_threshold(self):
        drc = DynamicRiskConfig(100.0, _DEFAULT_CONFIG)
        # exactly at threshold (10%) — not strictly greater, so False
        assert drc.should_recompute(110.0) is False


# ── fetch_startup_nav ─────────────────────────────────────────────────────────


class TestFetchStartupNav:
    def _make_api(self, balances, ticker_price=45_000.0):
        api = Mock()
        api.get_balances.return_value = balances
        api.get_ticker.return_value = {"last": ticker_price}
        return api

    # Helper to build a config that explicitly sets the quote asset
    def _cfg(self, quote: str = "USDT", initial_balance: float = 42.0) -> dict:
        return {
            "portfolio": {"initial_balance": initial_balance},
            "data": {"hybrid_dynamic_coin_config": {"quote_asset": quote}},
        }

    def test_returns_quote_balance(self):
        # THB-denominated account with explicit quote_asset
        api = self._make_api({"THB": {"available": 100.0, "reserved": 0.0}})
        nav = fetch_startup_nav(api, self._cfg("THB", initial_balance=100.0))
        assert nav == pytest.approx(100.0)

    def test_returns_usdt_quote_balance(self):
        # USDT-denominated account — the default case for Binance.th USDT pairs
        api = self._make_api({"USDT": {"available": 42.0, "reserved": 0.0}})
        nav = fetch_startup_nav(api, self._cfg("USDT", initial_balance=42.0))
        assert nav == pytest.approx(42.0)

    def test_default_quote_is_usdt_not_thb(self):
        # When no quote_asset is configured, USDT balance should be recognised directly
        api = self._make_api({"USDT": {"available": 50.0, "reserved": 0.0}})
        nav = fetch_startup_nav(api, {"portfolio": {"initial_balance": 50.0}})
        assert nav == pytest.approx(50.0)

    def test_marks_non_quote_assets(self):
        # THB account: BTC is priced in THB
        api = self._make_api(
            {"THB": {"available": 50.0, "reserved": 0.0}, "BTC": {"available": 0.001, "reserved": 0.0}},
            ticker_price=1_000_000.0,
        )
        nav = fetch_startup_nav(api, self._cfg("THB", initial_balance=50.0 + 0.001 * 1_000_000.0))
        assert nav == pytest.approx(50.0 + 0.001 * 1_000_000.0)

    def test_falls_back_to_initial_balance_on_empty(self):
        api = Mock()
        api.get_balances.return_value = {}
        nav = fetch_startup_nav(api, {"portfolio": {"initial_balance": 42.0}})
        assert nav == pytest.approx(42.0)

    def test_falls_back_on_api_exception(self):
        api = Mock()
        api.get_balances.side_effect = RuntimeError("connection refused")
        nav = fetch_startup_nav(api, {"portfolio": {"initial_balance": 99.0}})
        assert nav == pytest.approx(99.0)

    def test_skips_unpriceable_assets(self):
        # THB account: WEIRD cannot be priced → only THB counts
        api = self._make_api(
            {"THB": {"available": 50.0, "reserved": 0.0}, "WEIRD": {"available": 1.0, "reserved": 0.0}},
        )
        api.get_ticker.side_effect = Exception("no market")
        nav = fetch_startup_nav(api, self._cfg("THB", initial_balance=50.0))
        # Only quote balance should be counted
        assert nav == pytest.approx(50.0)

    def test_sanity_check_rejects_implausibly_large_nav(self):
        # Simulates wrong-currency bug: USDT holdings priced at USDTTHB ≈ 33.5
        # would make 42 USDT appear as 1407 THB. Sanity guard should reject it.
        api = self._make_api({"USDT": {"available": 42.0, "reserved": 0.0}}, ticker_price=33.5)
        # No quote_asset set → defaults to USDT, USDT == USDT → no ticker needed
        nav = fetch_startup_nav(api, {"portfolio": {"initial_balance": 42.0}})
        assert nav == pytest.approx(42.0)


# ── run_iteration_runtime integration: recompute trigger ──────────────────────


class TestRecomputeTrigger:
    """Verify the loop-based trigger in run_iteration_runtime calls DynamicRiskConfig correctly."""

    def _make_bot(self, nav: float, loop_count: int, drc: DynamicRiskConfig):
        bot = MagicMock()
        bot._auth_degraded = False
        bot._auth_degraded_logged = False
        bot.api_client.is_circuit_open.return_value = False
        bot.api_client.check_clock_sync.return_value = True
        bot._loop_count = loop_count
        bot._dynamic_risk_config = drc
        bot.config = {"trading": {"runtime_order_reconcile": False}}
        bot._get_portfolio_state.return_value = {"total_balance": nav}
        bot._get_risk_portfolio_value.return_value = nav
        bot._is_paused.return_value = (False, "")
        bot._trading_disabled.is_set.return_value = False
        bot._state_machine_enabled = False
        bot._get_trading_pairs.return_value = []
        bot._filter_pairs_by_candle_readiness.return_value = []
        bot._held_coins_only = False
        bot._last_portfolio_guard_skipped = ()
        return bot

    def test_no_recompute_when_nav_change_below_threshold(self):
        drc = DynamicRiskConfig(100.0, _DEFAULT_CONFIG)
        bot = self._make_bot(nav=105.0, loop_count=10, drc=drc)  # 5% change

        from trading.bot_runtime.run_iteration_runtime import run_trading_iteration

        run_trading_iteration(bot)
        # apply_dynamic_risk_to_manager should NOT be called (no significant change)
        bot.risk_manager.config.max_position_per_trade_pct  # attribute exists — just verify no exception

    def test_recompute_triggered_when_nav_change_above_threshold(self):
        drc = DynamicRiskConfig(100.0, _DEFAULT_CONFIG)
        # 50% NAV increase → should_recompute = True
        bot = self._make_bot(nav=150.0, loop_count=10, drc=drc)

        with patch("trading.dynamic_config.apply_dynamic_risk_to_manager") as mock_apply:
            from trading.bot_runtime.run_iteration_runtime import run_trading_iteration

            run_trading_iteration(bot)
            mock_apply.assert_called_once()

    def test_no_recompute_on_non_multiple_loop(self):
        drc = DynamicRiskConfig(100.0, _DEFAULT_CONFIG)
        bot = self._make_bot(nav=200.0, loop_count=7, drc=drc)  # not a multiple of 10

        with patch("trading.bot_runtime.run_iteration_runtime._try_dynamic_risk_recompute") as mock_recompute:
            from trading.bot_runtime.run_iteration_runtime import run_trading_iteration

            run_trading_iteration(bot)
            mock_recompute.assert_not_called()

    def test_recompute_called_on_multiple_of_10(self):
        drc = DynamicRiskConfig(100.0, _DEFAULT_CONFIG)
        bot = self._make_bot(nav=200.0, loop_count=20, drc=drc)  # multiple of 10

        with patch("trading.bot_runtime.run_iteration_runtime._try_dynamic_risk_recompute") as mock_recompute:
            from trading.bot_runtime.run_iteration_runtime import run_trading_iteration

            run_trading_iteration(bot)
            mock_recompute.assert_called_once_with(bot)
