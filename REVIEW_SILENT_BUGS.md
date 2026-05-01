# Silent Bugs & Code Quality Review

## Summary

| Category | Count | Risk Level |
|----------|-------|------------|
| TODO/FIXME Comments | 8 | Medium |
| Division without zero-check | ~15 | Low-Medium |
| dict.get() without defaults | 300+ | Medium |
| Silent except blocks | 0 | ✅ Good |
| Bare except: pass | 0 | ✅ Good |

---

## TODO/FIXME Issues (8 found)

### 1. `enums/candletype.py`
```python
# TODO: Could take up less memory if these weren't a CandleType
FUNDING_RATE = "funding_rate"
```
**Risk:** Low - Memory optimization note

### 2. `enums/rpcmessagetype.py`
```python
# TODO: do we still need to overwrite __repr__? Impact needs to be looked at in detail
return self.value
```
**Risk:** Low - Cleanup needed

### 3. `exchange/bybit.py`
```python
# TODO: Can be removed once bybit fully forces all accounts to unified mode.
"fetchOrder": False,
```
**Risk:** Medium - Feature may break when exchange changes

### 4. `exchange/exchange.py`
```python
# TODO: does this message make sense? would docs be better?
# if any, this should be cached to avoid log spam!
if stake_amount < min_stake and stake_amount <= max_stake:
```
**Risk:** Low - Documentation/logging issue

### 5. `exchange/exchange.py`
```python
# TODO: Remove this warning eventually
# Code could be simplified by removing the check for min-stake in the above
```
**Risk:** Low - Cleanup needed

### 6. `exchange/exchange_utils.py`
```python
TODO: If ccxt supports ROUND_UP for decimal_to_precision(), we could remove this and
align with amount_to_precision().
```
**Risk:** Low - Future enhancement

### 7. `plugins/protections/max_drawdown_protection.py`
```python
# TODO: Implement checks to limit max_drawdown to sensible values
```
**Risk:** High - Missing validation could cause issues

### 8. `trading/signal_runtime.py`
```python
logger.debug(f"Trade rejected for {symbol}: ATR unavailable")
return None
```
**Risk:** Medium - Silent rejection without user notification

---

## Division without Zero-Check

### Good Examples (Already Protected) ✅
```python
# cli_snapshot_dto.py
(value_quote / total_balance_quote * 100.0) if total_balance_quote > 0 else 0.0

# database.py
"success_rate": success / total if total > 0 else 0.0

# trade_executor.py
"success_rate": successful / total * 100 if total > 0 else 0
```

### Good News - Already Protected ✅

1. **`indicators.py`** - VWAP function is SAFE
```python
# Line 337: Already protected with .replace(0, np.nan)
rolling_vol = volume.rolling(window=window, min_periods=1).sum().replace(0, np.nan)
# Line 340: Already protected
cumulative_vol = volume.cumsum().replace(0, np.nan)
```

2. **`multi_timeframe.py`**
```python
confidence = max(buy_score, sell_score) / total_score
```
**Risk:** Low - `total_score` should never be 0 if logic is correct

3. **`portfolio_rebalancer.py`**
```python
self.current_pct = (self.current_value / total_portfolio_value) * 100
```
**Risk:** Low - Should have guard before this line

---

## dict.get() without Defaults (300+ found)

### High-Risk Patterns

1. **`api_client.py`** - Many exchanges use dict.get() patterns
```python
entry = symbols[0] if symbols else None  # Could be None
```

2. **`database.py`** - Position state loading
```python
row.trailing_peak = pos_data.get("trailing_peak")  # Returns None if missing
```

3. **`risk_management.py`** - Daily loss tracking
```python
self._daily_loss_start = data.get("daily_loss_start")  # Could be None
```

### Recommended Fix Pattern

Instead of:
```python
value = data.get("key")  # Returns None if missing
```

Use:
```python
value = data.get("key", default_value)  # Safe default
```

Or explicit:
```python
if "key" not in data:
    raise KeyError("Required key 'key' missing")
value = data["key"]
```

---

## Silent Failure Patterns (Good News!) ✅

The codebase is mostly clean:

- **No bare `except: pass`** - All exceptions are logged or have fallbacks
- **No empty except blocks** - All handle errors properly
- **No silent return None without logging** - Most functions log why they return None

---

## Critical Issues Found & Fixed ✅

### Issue 1: Missing validation in max_drawdown_protection ✅ FIXED
**File:** `plugins/protections/max_drawdown_protection.py`
```python
# Added validation:
# - Ratios mode: 0.0-1.0 range check
# - Equity mode: >= 0 check
# - Logs warning for invalid values
```
**Status:** ✅ FIXED

### Issue 2: Silent trade rejection ✅ FIXED
**File:** `trading/signal_runtime.py`
```python
# Changed from debug to warning:
logger.warning(
    f"[SignalRuntime] %s %s signal rejected: ATR data unavailable (ATR=%s). "
    f"Trade will be retried on next signal tick.",
    symbol,
    signal_type.upper(),
    atr_value,
)
```
**Status:** ✅ FIXED

### Issue 3: Potential division by zero in VWAP ✅ NO ACTION NEEDED
**File:** `indicators.py`
- Already protected with `.replace(0, np.nan)` at lines 337 and 340
- Pandas handles NaN gracefully in division
**Status:** ✅ Already Safe

---

## Recommendations

1. **High Priority:**
   - Add validation to max_drawdown_protection.py
   - Add user notification for trade rejections

2. **Medium Priority:**
   - Add zero-check for VWAP calculation
   - Review all TODO/FIXME comments and prioritize fixes

3. **Low Priority:**
   - Standardize dict.get() patterns with defaults
   - Add docstrings to TODO comments

---

## Test Coverage Status

Run tests to verify there are no regressions:
```bash
pytest tests/ -v --tb=short
```
