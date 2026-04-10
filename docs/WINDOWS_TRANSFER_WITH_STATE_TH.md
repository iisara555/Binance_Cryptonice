# Windows Transfer Checklist

คู่มือนี้ใช้สำหรับย้ายโปรเจกต์จาก Windows เครื่องเดิมไป Windows อีกเครื่อง โดยต้องการเอาไปทั้ง:

- bot runtime
- ฐานข้อมูลเดิม
- state เดิม
- config เดิม

เป้าหมายคือย้ายแล้วเปิดต่อบนเครื่องใหม่โดยยังเห็นข้อมูลเดิม และลดโอกาสเกิด duplicate trading หรือ state เพี้ยน

## ข้อห้ามก่อนเริ่ม

- ห้ามรัน bot พร้อมกัน 2 เครื่องด้วย API key ชุดเดียวกัน
- ห้าม copy ฐานข้อมูลตอน process ยังเขียนไฟล์อยู่

## 1. หยุดระบบบนเครื่องต้นทางให้สนิทก่อน

กรณีเปิดแบบ standalone:

```powershell
Set-Location "C:\path\to\crypto-bot V1"
Ctrl+C ในหน้าต่างที่รัน `main.py`, `run_bot.bat` หรือ `restart_bot.bat`
```

กรณีใช้ Windows services:

```powershell
Stop-Service CryptoBotRuntime
```

หรือถ้าต้องการรีสตาร์ตแบบควบคุมผ่าน script:

```powershell
Set-Location "C:\path\to\crypto-bot V1"
.\deploy\windows\restart-runtime-service.ps1
```

## 2. ไฟล์และโฟลเดอร์ที่ควรย้ายไปด้วย

### ต้องย้าย

- ทั้งโฟลเดอร์โปรเจกต์
- `.env` ของจริง
- `bot_config.yaml`
- `coin_whitelist.json`
- `crypto_bot.db`
- `risk_state.json`
- `balance_monitor_state.json`

### ย้ายถ้าต้องการเก็บประวัติเสริม

- log files ที่ต้องการเก็บ audit trail
- `logs/windows-service-health-state.json` ถ้าต้องการประวัติฝั่ง health monitor

### ถ้าหยุดระบบไม่ทันและจำเป็นต้องย้าย SQLite แบบ live snapshot

ให้ย้ายทั้ง 3 ไฟล์พร้อมกัน:

- `crypto_bot.db`
- `crypto_bot.db-wal`
- `crypto_bot.db-shm`

## 3. ไฟล์และโฟลเดอร์ที่ไม่ควรย้าย

- `.venv/`
- `.venv-3/`
- `venv/`
- `__pycache__/`
- `.pytest_cache/`

สิ่งเหล่านี้ควรสร้างใหม่บนเครื่องปลายทาง

ถ้าต้องการเก็บโฟลเดอร์ให้เล็กลงก่อน copy ให้รัน:

```powershell
Set-Location "C:\path\to\crypto-bot V1"
.\scripts\cleanup_before_transfer.ps1
```

## 4. เตรียมเครื่องใหม่

ต้องมีอย่างน้อย:

- Windows
- Python 3.10+
- PowerShell 5.1+

ถ้าจะใช้ Windows always-on services ด้วย ให้ติดตั้ง NSSM แยกบนเครื่องใหม่ด้วย อย่าย้าย service registration เดิมมาทั้งก้อน

## 5. วางโปรเจกต์ลงเครื่องใหม่

ตัวอย่าง:

```powershell
Set-Location "C:\Users\<YourUser>\Desktop"
```

จากนั้นวางโฟลเดอร์โปรเจกต์ เช่น:

```text
C:\Users\<YourUser>\Desktop\crypto-bot V1
```

## 6. สร้าง Python environment ใหม่

```powershell
Set-Location "C:\Users\<YourUser>\Desktop\crypto-bot V1"
python -m venv .venv-3
.\activate_env.ps1
pip install -r requirements.txt
```

## 7. ตรวจ `.env` บนเครื่องใหม่

ค่าที่สำคัญ:

- `BITKUB_API_KEY`
- `BITKUB_API_SECRET`
- `LIVE_TRADING`
- `LOG_LEVEL`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

ถ้าเป็น private API หรือ live mode ต้องเพิ่ม IP ของเครื่องใหม่ใน Bitkub allowlist ด้วย

## 8. เช็กว่าข้อมูลเดิมมาครบ

รายการที่ควรตรวจ:

- `crypto_bot.db` อยู่ใน root โปรเจกต์
- `risk_state.json` อยู่ใน root โปรเจกต์
- `balance_monitor_state.json` อยู่ใน root โปรเจกต์
- `coin_whitelist.json` อยู่ใน root โปรเจกต์ถ้าคุณใช้ runtime pair hot reload

## 9. ทดสอบเปิดเครื่องใหม่แบบปลอดภัยก่อน

```powershell
Set-Location "C:\Users\<YourUser>\Desktop\crypto-bot V1"
$env:BOT_STARTUP_TEST_MODE = "1"
.\.venv-3\Scripts\python.exe main.py
```

สิ่งที่ต้องดู:

- bot เปิดได้
- อ่านฐานข้อมูลได้
- health server ขึ้นได้
- ไม่มี error เรื่อง `.env` หรือ Bitkub auth ที่ไม่คาดไว้

## 10. เช็ก health endpoint หลังเปิดจริง

```powershell
Set-Location "C:\Users\<YourUser>\Desktop\crypto-bot V1"
.\.venv-3\Scripts\python.exe scripts\vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --json
```

## 11. ถ้าจะสลับเครื่องใช้งานจริง

ก่อนเปิด live บนเครื่องใหม่ ให้แน่ใจว่า:

- เครื่องเก่าหยุดแล้วจริง
- ไม่มี scheduled task หรือ service เก่าที่ยัง auto-restart bot อยู่
- IP allowlist ของ Bitkub ชี้มาที่เครื่องใหม่แล้ว
- `LIVE_TRADING=true` ถูกเปิดอย่างตั้งใจเท่านั้น

## 12. ถ้าใช้ Windows services บนเครื่องใหม่

ให้ติดตั้งใหม่จากโปรเจกต์บนเครื่องใหม่:

```powershell
Set-Location "C:\Users\<YourUser>\Desktop\crypto-bot V1"
.\deploy\windows\install-nssm-services.ps1 -NssmPath "C:\path\to\nssm.exe"
```

## Checklist สั้นสุด

1. หยุด bot บนเครื่องเก่าให้หมด
2. copy โฟลเดอร์โปรเจกต์ + `.env` + `crypto_bot.db` + `risk_state.json` + `balance_monitor_state.json`
3. สร้าง `.venv-3` ใหม่บนเครื่องใหม่
4. install `requirements.txt`
5. เพิ่ม IP เครื่องใหม่ใน Bitkub allowlist
6. ทดสอบด้วย `BOT_STARTUP_TEST_MODE=1`
7. รัน preflight ให้ผ่าน
8. ค่อยเปิดใช้งานจริงหลังยืนยันว่าเครื่องเก่าหยุดแล้ว
