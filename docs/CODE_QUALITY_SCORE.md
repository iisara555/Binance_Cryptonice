# Code Quality Score

## Status Of This Document

เอกสารฉบับนี้แทน scorecard รุ่นเก่าที่อ้างคะแนนคงที่, test count เก่า, และ subsystem ที่ไม่อยู่ใน repo ปัจจุบันแล้ว

แทนที่จะยึดเลขคะแนนแบบ snapshot เดียว เอกสารนี้สรุปคุณภาพเชิงโครงสร้างของ runtime ปัจจุบันและสิ่งที่ควรตรวจซ้ำก่อน deployment

## Current Quality Summary (Updated 2026-05-01)

### ✅ Strong Areas

- Runtime path resolution ถูกทำให้ portable ขึ้นสำหรับ standalone use
- `.env` และ config ที่สำคัญถูกโหลดจาก project root อย่างชัดเจน
- Telegram noise ถูกลดลงให้สื่อสารเฉพาะเหตุสำคัญมากขึ้น
- Position quantity normalization ถูกแก้ที่ต้นตอและรวม logic ซ้ำไว้ส่วนกลาง
- Rich terminal แสดง portfolio visibility ดีขึ้นทั้ง total balance, breakdown, allocation และ progress bars
- Windows runtime service scripts ยังมี health supervision ที่สอดคล้องกับ runtime ปัจจุบัน
- **Exception handling สะอาดมาก** - ไม่มี bare `except: pass` หรือ empty except blocks
- **Alert system รองรับ THB/USDT dynamic** - quote_asset parameter added

### ✅ Recently Fixed (Silent Bugs Review 2026-05-01)

| Issue | File | Status |
|-------|------|--------|
| max_drawdown_protection - ไม่มี validation | `plugins/protections/max_drawdown_protection.py` | ✅ Fixed |
| Silent trade rejection - ไม่แจ้ง user | `trading/signal_runtime.py` | ✅ Fixed |

### ✅ Division Operations - Safe

- **VWAP calculation** - มี `.replace(0, np.nan)` ป้องกันอยู่แล้ว
- **Win rate calculations** - มี zero-check guards ทั้งหมด
- **Success rate** - มี ternary guard ทุกที่

### ⚠️ Areas To Re-Review Periodically

- Order idempotency เมื่อเกิด network timeout ระหว่างส่ง order
- Crash recovery และ reconciliation หลัง restart
- SQLite durability ระหว่าง forced shutdown หรือ transfer state
- WebSocket-driven exit behavior ภายใต้ rate-limit pressure
- Windows service semantics หลังย้าย project root
- dict.get() patterns - 300+ occurrences, ส่วนใหญ่เป็น optional config (acceptable)

### ⚠️ TODO/FIXME Comments (8 found)

| # | File | Priority | Description |
|---|------|---------|-------------|
| 1 | enums/candletype.py | Low | Memory optimization note |
| 2 | enums/rpcmessagetype.py | Low | Cleanup needed |
| 3 | exchange/bybit.py | Medium | Feature may break when exchange changes |
| 4 | exchange/exchange.py (×2) | Low | Documentation/logging issue |
| 5 | exchange/exchange_utils.py | Low | Future enhancement |
| 6 | plugins/protections/max_drawdown_protection.py | ~~High~~ | ✅ Already Fixed |
| 7 | trading/signal_runtime.py | ~~Medium~~ | ✅ Already Fixed |

## Quality Position

runtime ปัจจุบันถือว่าใช้งานได้จริงสำหรับ standalone terminal-first workflow แต่ไม่ควรตีความว่า "คะแนนสูงครั้งหนึ่ง" เท่ากับปลอดภัยถาวร

คุณภาพของ repo นี้ควรถูกประเมินจาก:

- regression tests ล่าสุด
- health / preflight ล่าสุด
- docs ที่ตรงกับ codebase ปัจจุบัน
- operational behavior จริงหลัง restart, path move, และ service supervision

## Silent Bugs Summary

### Scan Results (2026-05-01)

| Category | Count | Risk Level | Status |
|----------|-------|------------|--------|
| Silent except blocks | 0 | ✅ Good | Clean |
| Bare except: pass | 0 | ✅ Good | Clean |
| TODO/FIXME Comments | 8 | Medium | 2 Fixed |
| Division without zero-check | ~15 | Low | All Safe |
| dict.get() without defaults | 300+ | Medium | False Positives |

### Recommendations

1. **High Priority:** ✅ Done
   - max_drawdown_protection validation - Fixed
   - signal_runtime silent rejection - Fixed

2. **Medium Priority:**
   - Review TODO/FIXME in bybit.py (exchange may force unified mode)
   - Standardize critical dict.get() patterns with explicit defaults

3. **Low Priority:**
   - Add docstrings to TODO comments
   - Create follow-up review in 3 months

## How To Validate Quality Now

### Targeted regression tests

```powershell
python -m pytest tests/test_integration.py tests/test_telegram.py -v
```

### Syntax / diagnostics

ใช้ editor diagnostics หรือ `pytest` หลังแก้ไฟล์ runtime สำคัญ

### Runtime smoke test

```powershell
$env:BOT_STARTUP_TEST_MODE = "1"
.\.venv-3\Scripts\python.exe main.py
```

### Operational check

```powershell
.\.venv-3\Scripts\python.exe scripts/vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --json
```

### Full Silent Bugs Report

ดูรายละเอียดที่: `REVIEW_SILENT_BUGS.md`
