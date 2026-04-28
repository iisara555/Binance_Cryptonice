# Security Audit Report

## Status Of This Document

เอกสารฉบับนี้ถูกปรับให้สอดคล้องกับ codebase ปัจจุบันแบบ runtime-only

รายงานรุ่นก่อนอ้าง subsystem ที่ถูกลบออกจาก repo แล้ว เช่น `ai_signals/`, dashboard และ frontend ดังนั้นการอ้างอิงเหล่านั้นไม่ควรถูกใช้เป็นฐานตัดสินใจสำหรับ deployment ปัจจุบัน

## Audit Scope For Current Repo

ไฟล์และระบบที่ยังอยู่ในขอบเขตจริงของ runtime ปัจจุบัน:

- `main.py`
- `trading_bot.py` และแพ็กเกจ orchestrator helpers ภายใต้ `trading/bot_runtime/`
- `trade_executor.py`
- `risk_management.py`
- `api_client.py`
- `database.py`
- `signal_generator.py`
- adapter ราคาเรียลไทม์ (เช่น `binance_websocket.py`, `bitkub_websocket.py` ถ้ามีใน environment)
- `portfolio_rebalancer.py`
- `balance_monitor.py`
- `state_management.py`
- `process_guard.py`
- `config.py`
- `watchdog.py`

## Current High-Level Security Position

### Strengths

- Fail-fast env validation for critical Bitkub credentials
- Public-only degraded mode when private auth is unavailable
- SQLite-backed persistence for orders, positions, and trade state
- Process guard / lock file to reduce accidental double-start
- Windows runtime health supervision scripts
- Telegram command polling can be disabled independently from outbound alerts

### Known Operational Risks To Keep In Mind

- Windows service registration is path-bound and must be reinstalled after moving the project root
- SQLite state still requires clean shutdown or proper transfer of `db`, `db-wal`, and `db-shm` when copying live state
- Any live deployment still depends on correct Bitkub IP allowlist and intentional `LIVE_TRADING` review
- `status=degraded` is a safe runtime mode, not a green light for live trading

## What Was Removed From The Old Audit

รายการต่อไปนี้ไม่ใช่ส่วนหนึ่งของ repo ปัจจุบันอีกแล้ว:

- AI/ML signal pipeline references
- `ai_signals/ensemble.py`
- `ai_signals/ai_signal_generator.py`
- dashboard-only attack surface
- frontend/static build concerns
- `start.py` launcher behavior

## Recommended Validation Before Live Mode

```powershell
.\.venv-3\Scripts\python.exe scripts/vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --json
```

และสำหรับ local smoke test:

```powershell
$env:BOT_STARTUP_TEST_MODE = "1"
.\.venv-3\Scripts\python.exe main.py
```

## Recommended Follow-Up Review Areas

- Runtime order idempotency across transient API/network failures
- Reconciliation correctness after crash recovery
- Rate-limit behavior during WebSocket-driven exits
- Windows service operational safety after project relocation
- SQLite durability during forced shutdown or transfer
