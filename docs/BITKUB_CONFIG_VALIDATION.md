# Bitkub Runtime Validation

## Purpose

เอกสารนี้แทน workflow รุ่นเก่าที่อ้าง `validate_bitkub_config.py` และ `show_bitkub_coins.py` ซึ่งไม่มีอยู่ใน repo ปัจจุบันแล้ว

สำหรับ repo ปัจจุบัน การ validate ก่อนเทรดควรอิง runtime จริงของบอท, health endpoint, และ preflight script ที่ยังมีอยู่

## Current Validation Workflow

### 1. Validate local config files

ยืนยันว่ามีไฟล์ต่อไปนี้ใน root:

- `.env`
- `bot_config.yaml`
- `coin_whitelist.json` ถ้าคุณใช้ runtime whitelist
- `crypto_bot.db` ถ้าต้องการใช้ state เดิม

### 2. Run safe startup

```powershell
$env:BOT_STARTUP_TEST_MODE = "1"
.\.venv-3\Scripts\python.exe main.py
```

สิ่งที่ต้องดู:

- บอทเปิดได้โดยไม่ crash
- `.env` ถูกโหลดได้
- ไม่มี fatal auth/config errors ที่ไม่ตั้งใจ
- health server เริ่มทำงานได้

### 3. Check runtime health

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health
```

ค่าที่สำคัญ:

- `healthy: true`
- `status: ok` สำหรับ live readiness
- ถ้าเป็น `status: degraded` แปลว่า private Bitkub auth ยังไม่พร้อม แม้ process จะยังรันแบบ safe mode ได้

### 4. Run preflight

```powershell
.\.venv-3\Scripts\python.exe scripts/vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --json
```

ถ้าตั้งใจตรวจใน degraded mode ชั่วคราว:

```powershell
.\.venv-3\Scripts\python.exe scripts/vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --allow-auth-degraded --json
```

## What To Review In bot_config.yaml

โฟกัสที่ section เหล่านี้:

- `trading.mode`
- `data.auto_detect_held_pairs`
- `data.pairs`
- `data.hybrid_dynamic_coin_config.*`
- `risk.*`
- `rebalance.*`
- `notifications.telegram_command_polling_enabled`
- `monitoring.health_check_port`
- `monitoring.health_check_path`

## Practical Validation Checklist

- Bitkub API keys are real, not placeholders
- Bitkub IP allowlist is correct
- `LIVE_TRADING` matches your intent
- `trading.mode` matches your intent
- Runtime health is reachable
- Preflight passes in strict mode before live trading

## Important Note

ถ้าคุณต้องการดูสถานะ holdings จริงใน runtime ปัจจุบัน ให้ดูจาก:

- Rich terminal panel `Portfolio Breakdown`
- bot health payload
- SQLite state และ logs

ไม่ใช่จาก utility scripts รุ่นเก่าที่ถูกลบออกจาก repo แล้ว
