import pytest

from portfolio_rebalancer import AllocationTarget, PortfolioRebalancer, ThresholdRebalanceStrategy


def test_env_target_allocation_applies_sideways_cash_shift(monkeypatch):
    monkeypatch.setenv("REBALANCE_TARGET_ALLOCATION", "THB:20,BTC:50,DOGE:30")

    rebalancer = PortfolioRebalancer(
        {
            "rebalance": {
                "enabled": True,
                "strategy": "threshold",
                "cash_assets": ["THB"],
                "sideways_exposure_factor": 0.5,
                "threshold": {
                    "threshold_pct": 10.0,
                    "min_rebalance_pct": 1.0,
                },
            }
        }
    )

    normal_targets = rebalancer.get_target_allocation()
    sideways_targets = rebalancer.get_target_allocation("SIDEWAY")

    assert normal_targets == {"THB": 20.0, "BTC": 50.0, "DOGE": 30.0}
    assert pytest.approx(sum(sideways_targets.values()), rel=1e-9) == 100.0
    assert sideways_targets["THB"] > normal_targets["THB"]
    assert sideways_targets["BTC"] < normal_targets["BTC"]
    assert sideways_targets["DOGE"] < normal_targets["DOGE"]


def test_threshold_strategy_returns_reason_when_within_threshold():
    strategy = ThresholdRebalanceStrategy(
        {
            "threshold_pct": 10.0,
            "min_rebalance_pct": 1.0,
        }
    )
    allocations = [
        AllocationTarget(symbol="BTC", target_pct=50.0, current_value=500.0),
        AllocationTarget(symbol="DOGE", target_pct=30.0, current_value=300.0),
        AllocationTarget(symbol="THB", target_pct=20.0, current_value=200.0),
    ]

    for allocation in allocations:
        allocation.calculate(1000.0)

    should_rebalance, reason = strategy.should_rebalance(allocations, {})

    assert should_rebalance is False
    assert reason == "Within threshold: max drift 0.00% (threshold: 10.0%)"
