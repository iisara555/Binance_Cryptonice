# Alert Logic Review - All Issues Resolved ✅

## Changes Applied (2026-05-05)

### Issue 1: Dual Polling Conflict ✅ FIXED
- **Root Cause:** `TelegramCommandHandler` (alerts.py) and `TelegramBotHandler` (telegram_bot.py) both polled `getUpdates` on the same bot token → HTTP 409 conflict → one handler silently died
- **Fix:** Removed `TelegramCommandHandler` entirely (dead code — never imported or called externally). `TelegramBotHandler` is the sole production handler.
- **Lines removed:** ~450 lines from `alerts.py` including `_COMMAND_HELP`, `get_commands()`, `COMMANDS`, `_maybe_await()`, and full `TelegramCommandHandler` class

### Issue 2: Wrong kwarg `value_thb` ✅ FIXED  
- **Location:** `trade_executor.py:2123`
- **Root Cause:** `format_trade_alert()` param is `value_quote`, not `value_thb`
- **Fix:** Changed to `value_quote=value_quote`

### Issue 3: OMS Bypasses AlertSystem ✅ FIXED
- **Location:** `trade_executor.py:2128`
- **Root Cause:** OMS fill verification used raw `TelegramSender.send()`, bypassing rate limiting
- **Fix:** Routes through `AlertSystem.send(AlertLevel.TRADE, msg)` when notifier is an AlertSystem; falls back to raw send for legacy

### Issue 4: SPEC_09 Events Bypass RateLimiter ✅ FIXED
- **Root Cause:** `_send_event()`, `send_entry_fill()`, `send_exit_fill()`, etc. called `asyncio.to_thread(telegram.send_message)` directly, bypassing `RateLimiter`
- **Fix:** Removed all SPEC_09 async methods — they were dead code (never called from any external module)

### Issue 5: TCH Auth Failure Not Terminal ✅ FIXED
- **Location:** `alerts.py:959`
- **Root Cause:** `TelegramCommandHandler._poll_once()` logged warnings on HTTP 401/403/409 but continued polling infinitely
- **Fix:** Added `self._running = False` on 401/403/409 to stop polling. Then removed entire class (Issue 1).

### Issue 6: Mixed parse_mode ✅ FIXED
- **Root Cause:** SPEC_09 events used Markdown while everything else used HTML
- **Fix:** Removed all Markdown-mode SPEC_09 code. All remaining alerts use HTML consistently.

### Issue 7: Unused imports ✅ FIXED
- Removed: `asyncio`, `aiohttp`, `Awaitable`, `Thread`, `timezone`
- These were only used by the removed SPEC_09/TCH code

## Summary Table

| Function | File | Status |
|----------|------|--------|
| `format_trade_alert` | alerts.py | ✅ OK — `value_quote` param, dynamic `quote_asset` |
| `format_trade_alert` | status_runtime.py | ✅ OK — uses `self.quote_asset()` |
| `format_exit_alert` | status_runtime.py | ✅ OK |
| `format_error_alert` | alerts.py | ✅ OK |
| `format_status_alert` | alerts.py | ✅ OK — dynamic `quote_asset` |
| `TelegramSender` | alerts.py | ✅ OK — sole transport layer |
| `AlertSystem` | alerts.py | ✅ OK — centralized routing with rate limiting |
| `TelegramBotHandler` | telegram_bot.py | ✅ OK — sole command handler |
| `TelegramCommandHandler` | alerts.py | 🗑️ REMOVED — caused 409 conflict |
| `_send_event` / SPEC_09 | alerts.py | 🗑️ REMOVED — dead code |
| `_COMMAND_HELP` | alerts.py | 🗑️ REMOVED — dead code |

## File Size Reduction
- `alerts.py`: 1,226 → 524 lines (**-702 lines**, -57%)
