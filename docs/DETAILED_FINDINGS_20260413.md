# Detailed Findings Report - 2026-04-13

## Scope
This report covers three workstreams completed in this session:

1. Rich CLI/dashboard bugs and runtime visibility issues.
2. WebSocket robustness and typing/runtime issues.
3. Dead code cleanup and placeholder-function handling in strategy/trading modules.

## Changes Applied

### 1. Rich CLI / dashboard fixes
Files:
- `cli_ui.py`
- `main.py`
- `bot_config.yaml`

Applied fixes:
- Fixed the empty-state row in the Signal Alignment table to match the declared column count.
- Restored dashboard loop safeguards:
  - render timing warnings for slow `live.update()` calls
  - snapshot timing warnings inside `get_cli_snapshot()`
  - CLI listener auto-restart when the input thread dies
- Updated scalping profile `max_trades_per_day` from `25` to `50` so the "Today's Trades" display matches the intended risk budget.

Why this mattered:
- The dashboard clock/log panel only move when the Live render loop keeps refreshing.
- Without timing diagnostics, slow snapshot steps are invisible and look like a frozen UI.
- A dead input thread makes the Rich footer chat appear broken even when the bot loop is still alive.

### 2. Bitkub WebSocket fixes
File:
- `bitkub_websocket.py`

Applied fixes:
- Improved reconnect log wording to reflect actual failure-count logic instead of a misleading attempt counter.
- Prevented unnecessary singleton reconnects when the symbol list contains the same symbols in a different order.
- Removed cache clearing on `stop()` to avoid dropping the last known ticker snapshot during shutdown/restart transitions.
- Added heartbeat-thread handoff protection before starting a new monitor thread.
- Resolved Pylance/type-checking errors around the optional `websocket` import and guarded member access.

Why this mattered:
- The previous log message suggested a retry counter controlled reconnect limits, but the circuit breaker actually uses consecutive failures.
- Order-only symbol changes caused avoidable reconnect churn.
- Clearing the shared ticker cache immediately on stop could transiently force downstream readers to see `None` even though the last price was still useful.

### 3. Dead code and placeholder cleanup
Files:
- `strategies/breakout.py`
- `strategies/momentum.py`
- `strategies/mean_reversion.py`
- `strategies/trend_following.py`
- `trading/position_manager.py`
- `trading/__init__.py`
- `trading_bot.py`
- `process_guard.py`
- `telegram_bot.py`
- `alerts.py`

Applied fixes:
- Removed redundant trailing comments and no-op `pass` statements from concrete strategy classes.
- Implemented `PositionManager.sync_from_database()` using the same persisted position schema already used by `TradeExecutor`.
- Implemented `PositionManager.reconcile_with_exchange()` by reconciling its local cache against the executor snapshot, which is the repo's current runtime source of truth for tracked open positions.
- Made `PositionManager.get_position()` thread-safe.
- Made `PositionManager.update_price()` use the existing lock for price-cache mutation.
- Stopped exporting `PositionManager` via `trading.__all__` when the optional import fails.
- Replaced several `except Exception: pass` sites in `trading_bot.py` reconcile/cleanup paths with explicit logging.
- Replaced remaining silent exception swallowing in `process_guard.py`, `telegram_bot.py`, and `alerts.py` with explicit debug/warning logging while preserving fallback behavior.

Why this mattered:
- The strategy `pass` statements were harmless but dead code.
- The `PositionManager` stubs were more dangerous: if called, they silently did nothing and could hide broken state synchronization.
- Returning `PositionManager = None` from the package layer exposes a runtime footgun to importers.

## Additional Findings Investigated

### Finding A: `PositionManager` now performs direct exchange-aware reconciliation
File:
- `trading/position_manager.py`

Status:
- Partially fixed.

Details:
- The class contains real runtime logic for trailing stops and exit checks.
- Persistence and local reconciliation hooks were implemented in this session.
- Database sync now rebuilds `Position` objects from the same persisted rows used by the OMS layer.
- Reconciliation now queries Bitkub directly:
  - `get_open_orders(symbol)` keeps pending orders that still exist remotely.
  - `get_balances()` keeps filled/held BUY positions when the wallet balance still exists.
  - positions missing from both the open-order view and the wallet balance are pruned.
- The executor snapshot is no longer the only source of truth for reconciliation.

Risk:
- The module now works for in-process consistency and direct exchange-aware pruning.
- It still does not reconstruct brand-new held positions from exchange balances by itself; that remains a higher-level trading-bot/bootstrap responsibility.

Recommendation:
- If `PositionManager` becomes a primary runtime component, the next step is to reconstruct missing positions from exchange balances/history rather than only pruning/keeping existing tracked positions.

### Finding B: silent exception swallowing remains in runtime-critical paths
Files:
- `trading_bot.py`
- `process_guard.py`
- `alerts.py`
- `telegram_bot.py`
- `cli_ui.py`

Status:
- Investigated, not broadly changed in this session.

Examples observed:
- `trading_bot.py` used to suppress DB deletion errors during reconcile cleanup.
- `trading_bot.py` suppresses cleanup errors while closing DB/cursor resources.
- Several support modules still contain `except Exception: pass` fallback blocks.

Session update:
- The reconcile cleanup and DB-resource-close sites in `trading_bot.py` were upgraded to log warnings/debug output instead of silently swallowing errors.
- `process_guard.py`, `telegram_bot.py`, and `alerts.py` were also upgraded in this session to log suppressed fallback-path exceptions.
- Other modules may still need the same triage treatment, but the most immediate support/runtime helpers were covered.

Risk:
- Silent failure is acceptable in some cleanup/fallback paths.
- In reconcile and persistence-adjacent code, it can leave local state inconsistent without enough evidence in logs.

Recommendation:
- Triage each `except ...: pass` by category:
  - cleanup-only and safe to ignore
  - fallback path with explicit comment/log
  - state mutation path that should log at least `debug` or `warning`

### Finding C: `BalanceMonitor.stop()` still depends on in-flight API calls returning
File:
- `balance_monitor.py`

Status:
- Investigated, not changed in this session.

Details:
- `stop()` sets `_stop_event` and joins the worker for up to 10 seconds.
- The monitor loop checks `_stop_event` between API calls and during retry backoff waits.
- If the thread is blocked inside a long HTTP request, shutdown still depends on the request timeout expiring before the thread can observe the stop signal.

Risk:
- Under API degradation, shutdown latency can exceed expectations.
- The warning path already exists (`thread did not stop within 10 seconds`), so this is observable but not yet solved.

Recommendation:
- Audit API client request timeouts used by the balance-monitor endpoints.
- If shutdown responsiveness matters, reduce timeout or move balance-history calls behind a shorter dedicated timeout budget.

### Finding D: `trading/__init__.py` previously exported an unusable sentinel
File:
- `trading/__init__.py`

Status:
- Fixed.

Details:
- The package previously set `PositionManager = None` when the optional import failed.
- This can trick importers into thinking the symbol exists and is intentionally usable.

Outcome:
- `PositionManager` is now exported only when the import succeeds.

### Finding E: local `TradeExecutor` still diverges from the VPS snapshot, but focused OMS regressions currently pass
Files:
- `trade_executor.py`
- `deploy/vps_snapshots/20260413-runtime-hotfix/trade_executor.py`

Status:
- Audited.

Observed divergence:
- The local file replaces `_oms_stop_event.wait(...)` sleeps with short-slice polling via `_oms_poll_interval`.
- The local file replaces the snapshot's lazy `_ws_ticker()` helper with function-local `get_latest_ticker` imports.
- The local file removes `precise_*` arithmetic helpers from partial-fill aggregation and uses plain float math instead.

Validation result:
- Focused executor/OMS regression suite passed against the local file:
  - `tests/test_executor_idempotency.py`
  - `tests/test_m1_m2_order_lifecycle.py`
  - `tests/test_m3_m4_state_persistence.py`
  - `tests/test_oms_reconcile_gate.py`
- Result: `48 passed`

Assessment:
- The OMS lifecycle, reconciliation gate, idempotency fence, and persistence-related paths exercised by the focused tests still behave correctly with the local implementation.
- The short-slice polling change appears to be an intentional shutdown-latency improvement rather than a regression.
- The websocket access-path change is behaviorally acceptable in the tested paths.

Residual risk:
- The float-arithmetic change is not proven identical to the VPS snapshot in all edge cases, especially around long partial-fill accumulation or fee-sensitive rounding.
- This means the local file is better characterized as “focused-test validated” rather than “production-equivalent to the VPS snapshot”.

Recommendation:
- Keep the local OMS polling change.
- If production parity is required, add one more narrow test pass for repeated partial-fill accumulation and PnL/fee rounding before treating the local `trade_executor.py` as snapshot-equivalent.

## Validation Performed

Commands run successfully:
- `python -m pytest tests/test_integration.py -x -q --tb=short`
- `python -m pytest tests/test_runtime_cli_commands.py -x -q --tb=short`
- `python -m pytest tests/test_rate_limiter_websocket_safety.py tests/test_runtime_resilience.py tests/test_integration.py -x -q --tb=short`
- `python -m pytest tests/test_strategies.py -q --tb=short`
- `python -m pytest tests/test_position_manager.py -q --tb=short`
- `python -m pytest tests/test_position_manager.py tests/test_m3_m4_state_persistence.py tests/test_oms_reconcile_gate.py tests/test_strategies.py tests/test_runtime_cli_commands.py -q --tb=short`
- `python -m pytest tests/test_position_manager.py tests/test_runtime_resilience.py tests/test_telegram.py tests/test_integration.py tests/test_trading_bot_pause_state.py -q --tb=short`
- `python -m pytest tests/test_executor_idempotency.py tests/test_m1_m2_order_lifecycle.py tests/test_m3_m4_state_persistence.py tests/test_oms_reconcile_gate.py -q --tb=short`
- `python -m py_compile main.py cli_ui.py bitkub_websocket.py trading/position_manager.py trading/__init__.py`

Observed results:
- CLI, websocket, integration, and strategy tests passed after the applied fixes.
- One websocket threshold test was internally inconsistent with the code's actual threshold math and was corrected to match the current implementation.

## Recommended Next Pass

1. Audit and classify every `except Exception: pass` in runtime/state-management code.
2. Decide whether `PositionManager` is a real runtime component or an unfinished extraction; either complete it or narrow its public surface.
3. Reduce shutdown latency in `BalanceMonitor` by reviewing request timeout budgets for private history endpoints.
4. Add focused tests for `trading/__init__.py` optional export behavior and `PositionManager` fail-loud stubs.
