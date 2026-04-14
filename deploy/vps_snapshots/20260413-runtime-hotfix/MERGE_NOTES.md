# VPS Snapshot Merge Notes

## Must restore before next VPS deploy

1. `main.py`
- Restore lightweight dashboard snapshot path.
- Snapshot uses `bot.get_status(lightweight=...)` and `_get_portfolio_state(allow_refresh=...)` in `get_cli_snapshot()`.
- Snapshot also keeps `allow_rest_fallback=False` during live dashboard paths to avoid REST-heavy refreshes.

2. `trading_bot.py`
- Restore `_multi_timeframe_status_cache`.
- Restore `_get_portfolio_state(self, allow_refresh: bool = True)`.
- Restore `_get_dashboard_multi_timeframe_status(self, allow_refresh: bool = True)`.
- Restore `get_status(self, lightweight: bool = False)` and its cached/non-refreshing dashboard path.

3. `cli_ui.py`
- Restore `_NOISY_INFO_LOGGERS` filtering.
- Restore non-blocking log buffer access with cached `_last_log_rows_snapshot`.
- Restore console handler muting/restoration used during Rich Live.

## Review separately before deploy

4. `trade_executor.py`
- Local file still appears to preserve the OMS lock fix by taking `orders_to_check` under `_orders_lock` and waiting outside the lock.
- However it diverges from the verified VPS runtime in several other ways:
  - removes `precise_*` arithmetic helpers
  - removes `_oms_stop_event`
  - changes websocket ticker access path
- Do not assume it is production-equivalent without separate validation.

## Optional diagnostics from VPS snapshot

5. `main.py`
- `_configure_faulthandler_logging()` and `[CLI PERF]` warnings were present in the verified VPS snapshot.
- Useful for future VPS debugging, but not required for the minimal freeze fix.
