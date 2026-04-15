import logging
from datetime import datetime

import pandas as pd

from signal_generator import AggregatedSignal, SignalGenerator, SignalRiskCheck
from strategy_base import MarketCondition, SignalType


def test_get_best_signal_logs_risk_drop_reasons(caplog):
    generator = SignalGenerator()
    signal = AggregatedSignal(
        symbol="THB_BTC",
        signal_type=SignalType.BUY,
        combined_confidence=0.72,
        avg_price=100.0,
        avg_risk_reward=1.5,
        risk_score=80.0,
        strategy_votes={"trend_following": 1},
        timestamp=datetime.now(),
        market_condition=MarketCondition.RANGING,
    )
    generator.generate_signals = lambda data, symbol, use_strategies=None: [signal]  # type: ignore[assignment]
    generator.check_risk = lambda signal, portfolio: SignalRiskCheck(  # type: ignore[assignment]
        passed=False,
        reasons=["Confidence too low", "Risk score too high"],
    )

    with caplog.at_level(logging.WARNING, logger="crypto-bot.signal"):
        result = generator.get_best_signal(pd.DataFrame({"close": [1.0, 2.0, 3.0]}), "THB_BTC", {"balance": 1000.0})

    assert result is None
    assert "all candidates failed risk checks" in caplog.text
    assert "Confidence too low" in caplog.text