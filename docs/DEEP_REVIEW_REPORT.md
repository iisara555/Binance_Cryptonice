# Deep Review Report

## Purpose

เอกสารนี้สรุปประเด็นเชิงวิศวกรรมที่ยังควรจับตาใน runtime ปัจจุบัน หลังจาก repo ถูกทำให้เป็น standalone runtime-only และตัด dashboard / AI-ML subsystem เก่าออกแล้ว

## Current Architecture Focus

รีวิวเชิงลึกควรโฟกัสที่ระบบที่ยังใช้งานจริง:

- runtime loop ใน `main.py`
- orchestration ใน `trading_bot.py`
- execution และ position tracking ใน `trade_executor.py`
- risk/state persistence ใน `risk_management.py` และ `state_management.py`
- Bitkub connectivity ใน `api_client.py`
- watchdog และ Windows service supervision
- Rich terminal visibility ใน `cli_ui.py`

## Findings Worth Re-Checking Regularly

### 1. Restart And Reconciliation Correctness

หลัง crash หรือ process restart ต้องยืนยันว่า:

- position ที่ persisted ถูกโหลดกลับถูกหน่วย
- state machine ไม่กลับมาในสถานะผิด
- exits ที่ค้างไม่หายจาก tracking

### 2. Order Execution Safety

ก่อนใช้เงินจริงมากขึ้น ควรทบทวนว่า:

- duplicate execution ถูกกันพอสำหรับ network timeout แล้วหรือยัง
- partial fills ถูก handle ต่อเนื่องหลัง restart ดีพอหรือยัง
- stale balances ไม่ทำให้ sizing ผิดในช่วงรอยต่อหลัง order fill

### 3. Runtime Health Semantics

`status=degraded` เป็น safe-mode signal ไม่ใช่ success signal สำหรับ live deployment

การ deploy จริงควรยืนยันเสมอว่า:

- health endpoint ตอบ `healthy: true`
- status ไม่ใช่ `degraded`
- preflight strict ผ่าน

### 4. Portability And Operations

repo ปัจจุบันรองรับการย้ายโฟลเดอร์หรือเปลี่ยน drive สำหรับ standalone use ได้ดีขึ้น แต่ยังมีข้อจำกัดเชิงระบบปฏิบัติการ:

- Windows service registration ต้อง reinstall หลังย้าย path
- shell session เก่าที่จำ path เดิมอาจต้องเปิดใหม่
- live state transfer ยังต้องระวัง SQLite WAL/SHM files

### 5. Human Factors

ความเสี่ยงเชิงปฏิบัติการยังมาจาก:

- เปิด `LIVE_TRADING` โดยไม่ตั้งใจ
- ใช้ config ที่ไม่ตรง intent
- ถือว่า `degraded` คือพร้อม live
- ย้ายโปรเจกต์แล้วใช้ service registration เดิม

## Recommended Review Routine

1. Run targeted tests after touching runtime-critical code.
2. Run startup smoke test with `BOT_STARTUP_TEST_MODE=1`.
3. Run strict preflight before any serious deployment.
4. Review Rich terminal and logs after startup and after restart.
5. Reinstall Windows services after project relocation.

## Bottom Line

repo ปัจจุบันอยู่ในสภาพที่ชัดเจนและตรงไปตรงมากว่าเดิมมาก เพราะ architectural surface ถูกลดเหลือ runtime หลักจริง ๆ แต่จุดเสี่ยงที่เหลืออยู่เป็นเรื่อง execution safety, restart correctness, และ deployment discipline มากกว่าความซับซ้อนของ subsystem เสริม
