# Alert Logic Review - Found Issues

## Issue 1: Quote Currency Mismatch (CRITICAL)

### Problem
มี 2 ไฟล์ที่ format trade alert แต่ใช้สกุลเงินต่างกัน:

**alerts.py `format_trade_alert()`** (line 643-684):
```python
f"Entry/Fill: <code>{price:,.2f}</code> THB"
```
→ ใช้ THB (บาทไทย)

**trading/status_runtime.py `format_trade_alert()`** (line 155-175):
```python
quote = self.quote_asset()  # Default: "USDT"
f"Fill Price <code>{fill_price:,.0f}</code> {quote}"
```
→ ใช้ USDT

### Impact
- ถ้า exchange เป็น Binance TH → แสดง THB ✓
- ถ้า exchange เป็น Binance global → แสดง USDT ✓  
- แต่ `format_trade_alert` ใน `alerts.py` จะใช้ THB ตลอด ไม่ว่าจะ exchange ไหน

---

## Issue 2: send_entry_fill / send_exit_fill Hardcoded USDT

**alerts.py** (line 462-538):
```python
async def send_entry_fill(...):
    msg = (
        f"Amount: `${amount_usdt:,.2f} USDT`\n"  # Hardcoded!
        ...
    )

async def send_exit_fill(...):
    msg = (
        f"PnL: `{pnl_pct:+.2f}%` (`{pnl_usdt:+.2f} USDT`)\n"  # Hardcoded!
        ...
    )
```

### Impact
- ฟังก์ชันใหม่เหล่านี้ hardcoded USDT ไม่รองรับ THB
- ควรรับ `quote_asset` เป็น parameter

---

## Issue 3: Telegram Commands Show USDT (FIXED ✅)

**Solution Applied:**
- เปลี่ยน `COMMANDS` dict เป็น dynamic ผ่าน `get_commands(quote_asset)`
- `_cmd_help()` อ่าน `quote_asset` จาก bot config แล้วแสดง currency ที่ถูกต้อง

```python
def get_commands(quote_asset: str = "USDT") -> Dict[str, str]:
    """Return commands dict with dynamic currency placeholder filled."""
    return {cmd: desc.format(quote=quote_asset) for cmd, desc in _COMMAND_HELP.items()}
```

---

## Summary Table (All Fixed ✅)

| Function | File | Currency | Dynamic? | Status |
|----------|------|----------|----------|--------|
| `format_trade_alert` | alerts.py | THB/USDT | ✅ Yes | ✅ Fixed |
| `format_trade_alert` | status_runtime.py | USDT (default) | ✅ Yes | ✅ OK |
| `format_exit_alert` | status_runtime.py | USDT (default) | ✅ Yes | ✅ OK |
| `send_entry_fill` | alerts.py | THB/USDT | ✅ Yes | ✅ Fixed |
| `send_exit_fill` | alerts.py | THB/USDT | ✅ Yes | ✅ Fixed |
| `format_error_alert` | alerts.py | None (status only) | N/A | ✅ OK |
| `format_status_alert` | alerts.py | THB/USDT | ✅ Yes | ✅ Fixed |
| `COMMANDS` | alerts.py | THB/USDT | ✅ Yes | ✅ Fixed |

---

## All Issues Resolved ✅

1. ✅ **สร้าง AlertConfig class** - ใช้ `quote_asset` parameter แทน
2. ✅ **แก้ไข alerts.py** - รับ `quote_asset` เป็น parameter ทุก function
3. ✅ **อัพเดท Telegram commands** - แสดงสกุลเงินที่ถูกต้องตาม exchange
