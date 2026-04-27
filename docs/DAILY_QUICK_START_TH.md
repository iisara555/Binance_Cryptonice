# Daily Quick Start

คู่มือสั้นมากสำหรับเปิดใช้งาน Crypto Bot V1 ในชีวิตประจำวัน

ไฟล์ที่ควรมีไว้ดูคู่กัน:

- `.env.example`
- `run_bot.bat`
- `restart_bot.bat`
- `deploy/systemd/crypto-bot-tmux.service`
- `docs/VPS_GO_LIVE_CHECKLIST_TH.md`
- `docs/WINDOWS_TRANSFER_WITH_STATE_TH.md`

## Local แบบปลอดภัย

```powershell
Set-Location "C:\path\to\crypto-bot V1"
$env:BOT_STARTUP_TEST_MODE = "1"
.\.venv-3\Scripts\python.exe main.py
```

ถ้าต้องการเปิดแบบ standalone จาก Windows โดยไม่ต้องพิมพ์ path ของ Python ทุกครั้ง ให้ใช้:

```powershell
Set-Location "C:\path\to\crypto-bot V1"
.\run_bot.bat
```

## เช็กว่า runtime ปกติไหม

```powershell
.\.venv-3\Scripts\python.exe scripts/vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --json
```

ถ้าคาดหวัง degraded mode ชั่วคราว:

```powershell
.\.venv-3\Scripts\python.exe scripts/vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --allow-auth-degraded --json
```

## URLs สำคัญ

- Bot health: `http://127.0.0.1:8080/health`

## ถ้าจะรันจริง

- ตรวจให้แน่ใจว่า `LIVE_TRADING=true` ถูกเปิดอย่างตั้งใจ
- ตรวจว่า Bitkub allowlist มี IP ของเครื่องนี้แล้ว
- ตรวจว่า bot health เป็น `status: ok` ไม่ใช่ `degraded`

## หยุดระบบ

- ถ้ารัน `main.py` เอง ให้กด `Ctrl+C`
- ถ้ารันผ่าน `run_bot.bat` หรือ `restart_bot.bat` ให้ปิดหน้าต่างนั้นหรือกด `Ctrl+C`
- ถ้ารันบน VPS แบบ Rich CLI + auto-start ให้ใช้ `systemctl stop crypto-bot-tmux`
- ถ้ารันบน Windows service mode ให้ใช้ `Stop-Service CryptoBotRuntime`

