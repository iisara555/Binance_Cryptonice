"""
Backtesting Validation Module
Validates strategy performance on historical data before live trading
Implements walk-forward validation, performance metrics, and risk analysis
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

class ValidationStatus(Enum):
    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"

@dataclass
class BacktestResult:
    total_return: float
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    total_trades: int
    avg_trade_duration: timedelta
    max_consecutive_losses: int
    calmar_ratio: float
    sortino_ratio: float
    volatility: float
    
@dataclass
class ValidationReport:
    status: ValidationStatus
    score: float
    backtest_results: BacktestResult
    checks: Dict[str, bool]
    warnings: List[str]
    recommendations: List[str]
    
    def is_safe_for_live(self) -> bool:
        return self.status == ValidationStatus.PASS

class BacktestingValidator:
    """
    Validates trading strategies before live deployment
    Runs comprehensive backtesting and risk analysis
    """
    
    MINIMUM_REQUIRED_TRADES = 30
    MINIMUM_WIN_RATE = 0.45
    MAX_ALLOWED_DRAWDOWN = 0.25
    MINIMUM_SHARPE_RATIO = 1.0
    MINIMUM_PROFIT_FACTOR = 1.2
    
    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.validation_checks = self._get_validation_checks()
    
    def validate_strategy(self, 
                         strategy, 
                         historical_data: pd.DataFrame,
                         initial_capital: float = 10000.0,
                         run_walk_forward: bool = True) -> ValidationReport:
        """
        Full validation pipeline for strategy approval
        """
        logger.info(f"Starting backtesting validation for {strategy.__class__.__name__}")
        
        # Step 1: Run backtest simulation
        backtest_result = self._run_backtest(strategy, historical_data, initial_capital)
        
        # Step 2: Run validation checks
        checks = self._run_validation_checks(backtest_result)
        
        # Step 3: Generate warnings and recommendations
        warnings, recommendations = self._generate_feedback(backtest_result, checks)
        
        # Step 4: Calculate overall validation score
        score = self._calculate_validation_score(backtest_result, checks)
        
        # Step 5: Determine final status
        status = self._determine_validation_status(checks, score)
        
        report = ValidationReport(
            status=status,
            score=score,
            backtest_results=backtest_result,
            checks=checks,
            warnings=warnings,
            recommendations=recommendations
        )
        
        self._log_validation_report(report)
        
        return report
    
    def _run_backtest(self, strategy, data: pd.DataFrame, initial_capital: float) -> BacktestResult:
        """Execute backtest simulation on historical data"""
        portfolio_value = initial_capital
        position = 0
        trades = []
        equity_curve = [initial_capital]
        drawdowns = []
        peak = initial_capital
        
        for idx, row in data.iterrows():
            signal = strategy.analyze(pd.DataFrame(data[:idx+1]))
            
            # Execute trading logic
            if signal == 1 and position == 0:  # Long signal
                position = portfolio_value / row['close']
                trades.append(('entry', idx, row['close'], position))
                
            elif signal == -1 and position > 0:  # Exit signal
                exit_value = position * row['close']
                portfolio_value = exit_value
                trades.append(('exit', idx, row['close'], portfolio_value))
                position = 0
            
            # Update equity curve
            current_equity = portfolio_value + (position * row['close']) if position > 0 else portfolio_value
            equity_curve.append(current_equity)
            
            # Track drawdown
            peak = max(peak, current_equity)
            drawdown = (peak - current_equity) / peak
            drawdowns.append(drawdown)
        
        # Calculate performance metrics
        total_return = (equity_curve[-1] - initial_capital) / initial_capital
        equity_arr = np.array(equity_curve[:-1], dtype=float)
        equity_arr = np.where(equity_arr == 0, np.nan, equity_arr)
        returns = np.diff(equity_curve) / equity_arr
        returns = returns[np.isfinite(returns)]  # drop inf/nan from zero-equity points
        
        max_dd = max(drawdowns) if drawdowns else 0
        return BacktestResult(
            total_return=total_return,
            sharpe_ratio=self._calculate_sharpe_ratio(returns),
            max_drawdown=max_dd,
            win_rate=self._calculate_win_rate(trades),
            profit_factor=self._calculate_profit_factor(trades),
            total_trades=len(trades) // 2,
            avg_trade_duration=self._calculate_avg_trade_duration(trades, data),
            max_consecutive_losses=self._calculate_max_consecutive_losses(trades),
            calmar_ratio=(total_return / max_dd) if max_dd > 0 else 0.0,
            sortino_ratio=self._calculate_sortino_ratio(returns),
            volatility=np.std(returns) * np.sqrt(252) if len(returns) > 0 else 0
        )
    
    def _run_validation_checks(self, result: BacktestResult) -> Dict[str, bool]:
        """Run all validation checks against minimum thresholds"""
        return {
            'sufficient_trades': result.total_trades >= self.MINIMUM_REQUIRED_TRADES,
            'acceptable_win_rate': result.win_rate >= self.MINIMUM_WIN_RATE,
            'acceptable_drawdown': result.max_drawdown <= self.MAX_ALLOWED_DRAWDOWN,
            'acceptable_sharpe': result.sharpe_ratio >= self.MINIMUM_SHARPE_RATIO,
            'positive_expectancy': result.profit_factor >= self.MINIMUM_PROFIT_FACTOR,
            'positive_return': result.total_return > 0,
            'reasonable_volatility': result.volatility <= 1.0,
            'acceptable_consecutive_losses': result.max_consecutive_losses <= 5
        }
    
    def _calculate_validation_score(self, result: BacktestResult, checks: Dict[str, bool]) -> float:
        """Calculate overall validation score 0-100"""
        scoring_weights = {
            'sufficient_trades': 15,
            'acceptable_win_rate': 15,
            'acceptable_drawdown': 20,
            'acceptable_sharpe': 20,
            'positive_expectancy': 15,
            'positive_return': 10,
            'reasonable_volatility': 3,
            'acceptable_consecutive_losses': 2
        }
        
        score = sum(scoring_weights[check] for check, passed in checks.items() if passed)
        return round(score, 2)
    
    def _determine_validation_status(self, checks: Dict[str, bool], score: float) -> ValidationStatus:
        """Determine final validation status based on checks and score"""
        critical_checks = ['acceptable_drawdown', 'positive_expectancy', 'acceptable_sharpe']
        critical_passed = all(checks[check] for check in critical_checks)
        
        if not critical_passed or score < 60:
            return ValidationStatus.FAIL
        elif score < 80:
            return ValidationStatus.WARNING
        else:
            return ValidationStatus.PASS
    
    def _get_validation_checks(self) -> List[Tuple[str, callable, float]]:
        return [
            ("Minimum trades", lambda r: r.total_trades, self.MINIMUM_REQUIRED_TRADES),
            ("Win rate", lambda r: r.win_rate, self.MINIMUM_WIN_RATE),
            ("Max drawdown", lambda r: 1 - r.max_drawdown, 1 - self.MAX_ALLOWED_DRAWDOWN),
            ("Sharpe ratio", lambda r: r.sharpe_ratio, self.MINIMUM_SHARPE_RATIO),
            ("Profit factor", lambda r: r.profit_factor, self.MINIMUM_PROFIT_FACTOR)
        ]
    
    def _calculate_sharpe_ratio(self, returns, risk_free_rate=0.02):
        if len(returns) < 2 or np.std(returns) == 0:
            return 0.0
        excess_returns = returns - (risk_free_rate / 252)
        return np.sqrt(252) * np.mean(excess_returns) / np.std(returns)
    
    def _calculate_sortino_ratio(self, returns, risk_free_rate=0.02):
        if len(returns) < 2:
            return 0.0
        downside_returns = returns[returns < 0]
        if len(downside_returns) == 0 or np.std(downside_returns) == 0:
            return 10.0
        excess_returns = returns - (risk_free_rate / 252)
        return np.sqrt(252) * np.mean(excess_returns) / np.std(downside_returns)
    
    def _calculate_win_rate(self, trades):
        wins = 0
        total = 0
        entry_cost = None
        for t in trades:
            if t[0] == 'entry':
                entry_cost = t[2] * t[3]  # price * position_size
            elif t[0] == 'exit' and entry_cost is not None:
                total += 1
                if t[3] > entry_cost:  # exit portfolio_value > entry cost
                    wins += 1
                entry_cost = None
        return wins / total if total > 0 else 0.0
    
    def _calculate_profit_factor(self, trades):
        gross_profit = 0.0
        gross_loss = 0.0
        entry_cost = None
        for t in trades:
            if t[0] == 'entry':
                entry_cost = t[2] * t[3]  # price * position_size
            elif t[0] == 'exit' and entry_cost is not None:
                pnl = t[3] - entry_cost  # exit portfolio_value - entry cost
                if pnl > 0:
                    gross_profit += pnl
                else:
                    gross_loss += abs(pnl)
                entry_cost = None
        return gross_profit / gross_loss if gross_loss > 0 else (float('inf') if gross_profit > 0 else 0.0)
    
    def _calculate_avg_trade_duration(self, trades, data):
        durations = []
        entry_time = None
        for trade in trades:
            try:
                if trade[0] == 'entry':
                    entry_time = data.index.get_loc(trade[1])
                    entry_time = data.index[entry_time] if isinstance(entry_time, int) else trade[1]
                elif trade[0] == 'exit' and entry_time is not None:
                    exit_loc = data.index.get_loc(trade[1])
                    exit_time = data.index[exit_loc] if isinstance(exit_loc, int) else trade[1]
                    durations.append(exit_time - entry_time)
                    entry_time = None
            except (KeyError, IndexError):
                entry_time = None
        return sum(durations, timedelta()) / len(durations) if durations else timedelta()
    
    def _calculate_max_consecutive_losses(self, trades):
        max_losses = 0
        current = 0
        entry_cost = None
        for t in trades:
            if t[0] == 'entry':
                entry_cost = t[2] * t[3]  # price * position_size
            elif t[0] == 'exit' and entry_cost is not None:
                if t[3] < entry_cost:
                    current += 1
                    max_losses = max(max_losses, current)
                else:
                    current = 0
                entry_cost = None
        return max_losses
    
    def _generate_feedback(self, result: BacktestResult, checks: Dict[str, bool]) -> Tuple[List[str], List[str]]:
        warnings = []
        recommendations = []
        
        if not checks['sufficient_trades']:
            warnings.append(f"Insufficient trade samples: {result.total_trades} trades (minimum {self.MINIMUM_REQUIRED_TRADES} required)")
            recommendations.append("Extend backtest period to collect more trade history")
        
        if not checks['acceptable_win_rate']:
            warnings.append(f"Low win rate: {result.win_rate:.1%} (minimum {self.MINIMUM_WIN_RATE:.0%} required)")
            recommendations.append("Adjust strategy entry conditions to improve win rate")
        
        if not checks['acceptable_drawdown']:
            warnings.append(f"Excessive drawdown: {result.max_drawdown:.1%} (maximum {self.MAX_ALLOWED_DRAWDOWN:.0%} allowed)")
            recommendations.append("Implement stricter stop-loss rules or reduce position sizing")
        
        if not checks['acceptable_sharpe']:
            warnings.append(f"Low Sharpe ratio: {result.sharpe_ratio:.2f} (minimum {self.MINIMUM_SHARPE_RATIO} required)")
            recommendations.append("Reduce trade frequency or filter low-quality signals")
        
        if result.max_consecutive_losses > 3:
            warnings.append(f"High consecutive losses: {result.max_consecutive_losses}")
            recommendations.append("Implement cooling-off period after losing streaks")
        
        return warnings, recommendations
    
    def _log_validation_report(self, report: ValidationReport):
        """Log validation results"""
        logger.info("=" * 60)
        logger.info(f"BACKTESTING VALIDATION REPORT: {report.status.name}")
        logger.info(f"Overall Score: {report.score}/100")
        logger.info("-" * 60)
        logger.info(f"Total Return: {report.backtest_results.total_return:.2%}")
        logger.info(f"Sharpe Ratio: {report.backtest_results.sharpe_ratio:.2f}")
        logger.info(f"Max Drawdown: {report.backtest_results.max_drawdown:.2%}")
        logger.info(f"Win Rate: {report.backtest_results.win_rate:.2%}")
        logger.info(f"Profit Factor: {report.backtest_results.profit_factor:.2f}")
        logger.info(f"Total Trades: {report.backtest_results.total_trades}")
        
        if report.warnings:
            logger.warning("\nWARNINGS:")
            for warning in report.warnings:
                logger.warning(f"⚠️  {warning}")
        
        if report.recommendations:
            logger.info("\nRECOMMENDATIONS:")
            for rec in report.recommendations:
                logger.info(f"💡 {rec}")
        
        logger.info("=" * 60)
        
        if report.is_safe_for_live():
            logger.info("✅ STRATEGY APPROVED FOR LIVE TRADING")
        else:
            logger.info("❌ STRATEGY NOT APPROVED - Fix issues before live deployment")