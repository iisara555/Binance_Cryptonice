# Code Quality Score

## Status Of This Document

เอกสารฉบับนี้แทน scorecard รุ่นเก่าที่อ้างคะแนนคงที่, test count เก่า, และ subsystem ที่ไม่อยู่ใน repo ปัจจุบันแล้ว

แทนที่จะยึดเลขคะแนนแบบ snapshot เดียว เอกสารนี้สรุปคุณภาพเชิงโครงสร้างของ runtime ปัจจุบันและสิ่งที่ควรตรวจซ้ำก่อน deployment

## Current Quality Summary

### Strong Areas

- Runtime path resolution ถูกทำให้ portable ขึ้นสำหรับ standalone use
- `.env` และ config ที่สำคัญถูกโหลดจาก project root อย่างชัดเจน
- Telegram noise ถูกลดลงให้สื่อสารเฉพาะเหตุสำคัญมากขึ้น
- Position quantity normalization ถูกแก้ที่ต้นตอและรวม logic ซ้ำไว้ส่วนกลาง
- Rich terminal แสดง portfolio visibility ดีขึ้นทั้ง total balance, breakdown, allocation และ progress bars
- Windows runtime service scripts ยังมี health supervision ที่สอดคล้องกับ runtime ปัจจุบัน

### Areas To Re-Review Periodically

- Order idempotency เมื่อเกิด network timeout ระหว่างส่ง order
- Crash recovery และ reconciliation หลัง restart
- SQLite durability ระหว่าง forced shutdown หรือ transfer state
- WebSocket-driven exit behavior ภายใต้ rate-limit pressure
- Windows service semantics หลังย้าย project root

## How To Validate Quality Now

### Targeted regression tests

```powershell
python -m pytest tests/test_integration.py tests/test_project_paths.py tests/test_vps_preflight.py
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

## Quality Position

runtime ปัจจุบันถือว่าใช้งานได้จริงสำหรับ standalone terminal-first workflow แต่ไม่ควรตีความว่า “คะแนนสูงครั้งหนึ่ง” เท่ากับปลอดภัยถาวร

คุณภาพของ repo นี้ควรถูกประเมินจาก:

- regression tests ล่าสุด
- health / preflight ล่าสุด
- docs ที่ตรงกับ codebase ปัจจุบัน
- operational behavior จริงหลัง restart, path move, และ service supervision

