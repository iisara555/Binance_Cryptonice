# Crypto Bot V1

Crypto Bot V1 คือบอทเทรด Bitkub แบบ standalone ที่โฟกัส runtime ฝั่ง terminal เป็นหลัก โดยมีระบบ strategy, risk management, execution, portfolio rebalance, balance monitoring, health endpoint, Telegram alerts และเครื่องมือ preflight สำหรับตรวจความพร้อมก่อนใช้งานจริง

repo ปัจจุบันไม่มี dashboard, frontend, `start.py` หรือ AI/ML runtime เดิมแล้ว เอกสารฉบับนี้อ้างอิงเฉพาะไฟล์และ workflow ที่ยังมีอยู่จริงในโปรเจกต์

ถ้าต้องการคู่มือสั้นมากสำหรับใช้งานทุกวัน ให้ดู [docs/DAILY_QUICK_START_TH.md](docs/DAILY_QUICK_START_TH.md)

## โปรเจกต์นี้มีอะไรบ้าง

- Bitkub trading bot ที่มี collector, execution, risk management และ reconciliation
- รองรับหลายคู่เหรียญจาก holdings และ whitelist runtime
- SQLite persistence สำหรับ positions, trades และ state ต่าง ๆ
- ระบบ Telegram alerts และ command polling แบบเลือกเปิดได้
- Balance monitor และ portfolio rebalancing
- Health endpoint และ preflight script สำหรับ local, Windows always-on และ VPS

## โครงสร้างโปรเจกต์

```text
.
|- main.py                          # Entry point หลักของ trading bot
|- run_bot.bat                      # Portable Windows launcher
|- restart_bot.bat                  # Wrapper สำหรับ restart loop
|- activate_env.ps1                 # Portable PowerShell venv activation
|- activate_env.bat                 # Portable cmd venv activation
|- bot_config.yaml                  # Runtime configuration หลัก
|- config.py                        # Critical settings ที่โหลดจาก environment
|- cli_ui.py                        # Rich terminal command center
|- logger_setup.py                  # Shared logging stack
|- deploy/windows/                  # Windows NSSM service scripts
|- deploy/systemd/                  # Linux systemd templates
|- scripts/vps_preflight.py         # ตัวตรวจ readiness ของระบบ
|- strategies/                      # Technical trading strategies
|- tests/                           # Pytest suite
\- docs/                            # เอกสารประกอบการใช้งานและ deploy
```

## ความต้องการของระบบ

- Python 3.10+
- Bitkub API key และ secret ที่ตั้ง allowlist IP ถูกต้องแล้ว ถ้าจะใช้ private API หรือ live mode
- PowerShell บน Windows ถ้าจะใช้ตัวอย่างคำสั่งใน README นี้ตรง ๆ
- ถ้าจะใช้ Windows service mode ต้องมี NSSM (`nssm.exe`) และสิทธิ Administrator

## การตั้งค่า

### 1. ไฟล์ `.env` ที่ root โปรเจกต์

เริ่มจากคัดลอก `.env.example` ไปเป็น `.env`

```powershell
Copy-Item .env.example .env
```

จากนั้นแก้ค่าใน `.env` ให้เป็นของจริง

```env
BITKUB_API_KEY=your_real_bitkub_key
BITKUB_API_SECRET=your_real_bitkub_secret
LIVE_TRADING=false
LOG_LEVEL=INFO
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

หมายเหตุ:

- `BITKUB_API_KEY` และ `BITKUB_API_SECRET` เป็นค่าบังคับ ถ้าหายหรือเป็น placeholder ระบบจะ fail-fast ตอน startup
- `LIVE_TRADING=false` คือค่าที่ปลอดภัยกว่าในช่วงเริ่มต้น
- `TELEGRAM_BOT_TOKEN` และ `TELEGRAM_CHAT_ID` เป็น optional ถ้าไม่ตั้ง Telegram ระบบยังรันได้

### 2. ไฟล์ `bot_config.yaml`

ก่อนรันระบบ ควรเปิดดูอย่างน้อย section เหล่านี้

- `trading.mode`: `dry_run`, `semi_auto`, `full_auto`
- `data.*`: การเลือก runtime pairs และ hybrid whitelist
- `multi_timeframe.*`: การวิเคราะห์หลาย timeframe
- `risk.*`: daily loss, max position, max open positions
- `rebalance.*`: พฤติกรรม portfolio rebalance
- `monitoring.health_check_port` และ `monitoring.health_check_path`: ค่าของ bot health endpoint
- `notifications.telegram_command_polling_enabled`: ปิด long-poll command ได้โดยไม่ปิด outbound alerts

ดูรายละเอียด field ทั้งหมดได้ที่ [docs/CONFIGURATION_SCHEMA.md](docs/CONFIGURATION_SCHEMA.md)

## การติดตั้ง

```powershell
python -m venv .venv-3
.\activate_env.ps1
pip install -r requirements.txt
```

ถ้าต้องการเครื่องมือสำหรับ test และ code quality เพิ่ม:

```powershell
pip install -r requirements-dev.txt
```

## วิธีรัน

### Safe smoke test

ใช้โหมดนี้สำหรับตรวจระบบแบบปลอดภัยที่สุดใน local รอบแรก มันจะบังคับ `dry_run`, `read_only` และปิด Telegram polling ให้

```powershell
$env:BOT_STARTUP_TEST_MODE = "1"
.\.venv-3\Scripts\python.exe main.py
```

### Standalone bot

รัน bot จาก root ของโปรเจกต์ด้วย launcher ที่ portable ที่สุด:

```powershell
.\run_bot.bat
```

ถ้าต้องการเรียก Python โดยตรง:

```powershell
.\.venv-3\Scripts\python.exe main.py
```

ถ้าต้องการให้ wrapper พยายามเปิด bot ใหม่อัตโนมัติหลัง process หลุด:

```powershell
.\restart_bot.bat
```

หมายเหตุด้าน portability:

- launcher และ path resolution ของ runtime จะอิงตำแหน่งไฟล์ในโปรเจกต์ ไม่อิงชื่อโฟลเดอร์หรือ drive letter เดิม
- ถ้าย้ายทั้งโฟลเดอร์ไป path ใหม่บนเครื่องเดิม ให้ใช้ `.\run_bot.bat` หรือ `.\activate_env.ps1` / `.\activate_env.bat`
- ถ้าคุณติดตั้ง Windows service/NSSM ไปแล้ว การย้ายโฟลเดอร์ยังต้องรัน installer ใหม่ เพราะ service registry ของ Windows เก็บ absolute path

### Windows always-on service mode

ถ้าต้องการให้ runtime กลับมาหลัง reboot หรือ crash อัตโนมัติบน Windows ให้ใช้โครง `deploy/windows`

สคริปต์ที่เกี่ยวข้อง:

- `deploy/windows/run-runtime.ps1`
- `deploy/windows/install-nssm-services.ps1`
- `deploy/windows/invoke-health-check.ps1`
- `deploy/windows/restart-runtime-service.ps1`
- `deploy/windows/uninstall-nssm-services.ps1`

quick start:

```powershell
Set-Location "C:\path\to\crypto-bot-v1"

.\deploy\windows\install-nssm-services.ps1 \
  -NssmPath "C:\nssm\win64\nssm.exe"
```

ดูรายละเอียดเต็มได้ที่ [docs/WINDOWS_ALWAYS_ON_SETUP_TH.md](docs/WINDOWS_ALWAYS_ON_SETUP_TH.md)

หมายเหตุ: `deploy/windows/restart-service-pair.ps1` ยังอยู่เพื่อ compatibility กับ workflow เดิม แต่ชื่อที่ควรใช้ต่อจากนี้คือ `restart-runtime-service.ps1`

### Linux / VPS service mode

repo นี้มี systemd template สำหรับ runtime ที่ [deploy/systemd/crypto-bot-runtime.service](deploy/systemd/crypto-bot-runtime.service)

ตัวอย่างติดตั้งบน VPS:

```bash
cd /opt
git clone <your-repo-url> crypto-bot-v1
cd /opt/crypto-bot-v1
python3.10 -m venv .venv-3
source .venv-3/bin/activate
pip install -r requirements.txt
sudo cp deploy/systemd/crypto-bot-runtime.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable crypto-bot-runtime
sudo systemctl start crypto-bot-runtime
```

## Runtime states

### `dry_run`

- โหมดทดสอบที่ปลอดภัย
- ไม่ควรมีการวาง order จริง

### `semi_auto`

- ระบบยังสร้าง signal และ alert
- พฤติกรรมการเทรดขึ้นกับ approval flow ที่ตั้งไว้

### `full_auto`

- โหมด automated trading แบบ live-capable
- ถ้า `LIVE_TRADING=true` และ bot ไม่ได้เป็น read-only ระบบสามารถวาง order จริงได้

### `auth_degraded`

- เกิดขึ้นเมื่อ Bitkub private API ใช้งานไม่ได้ เช่น IP ยังไม่ได้อยู่ใน allowlist
- bot จะยังรันอยู่ใน public-only safe mode
- health endpoint จะรายงาน `status=degraded`
- ห้ามตีความว่าเป็นพร้อม live trading

## Health endpoint

### Bot health

- `GET http://127.0.0.1:8080/health`

endpoint นี้อ้างอิงค่าจาก `monitoring.health_check_port` และ `monitoring.health_check_path` ใน `bot_config.yaml`

ใช้ดู:

- โหมดการทำงานและความพร้อมของ bot
- สถานะ auth-degraded ถ้า Bitkub private API ใช้ไม่ได้
- state ของ runtime loop, risk summary และ service health

## Preflight checks

รัน strict readiness check:

```powershell
.\.venv-3\Scripts\python.exe scripts/vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --json
```

ถ้าคุณตั้งใจตรวจระบบใน public-only degraded mode ชั่วคราว:

```powershell
.\.venv-3\Scripts\python.exe scripts/vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --allow-auth-degraded --json
```

strict form ควรใช้เป็น go-live gate จริงเสมอ

## Logging และ observability

artifact หลักจะอยู่ใต้ `logs/`

ไฟล์สำคัญ:

- `logs/debug.log`: structured runtime log ทั่วไป
- `logs/services/runtime-service.log`: stdout ของ runtime service บน Windows
- `logs/services/runtime-service.err.log`: stderr ของ runtime service บน Windows
- `logs/windows-service-health-monitor.log`: log ของ health monitor บน Windows
- `logs/windows-service-health-state.json`: state ของ Windows health monitor

## Telegram

Telegram เป็น optional feature แต่ระบบรองรับทั้ง alerts และ command polling

พฤติกรรมที่สำคัญ:

- ถ้าไม่มี Telegram credentials ระบบยังรันได้ปกติ
- ถ้าต้องการให้ยังส่ง alert ได้แต่ไม่รับ command ให้ตั้ง `notifications.telegram_command_polling_enabled: false`
- ถ้าต้องการ communication แบบสำคัญเท่านั้น ให้ควบคุมจาก config และ alert routing ที่มีอยู่ใน runtime

## การทดสอบ

รัน test ทั้งหมด:

```powershell
python -m pytest
```

รันเฉพาะ readiness และ runtime regressions:

```powershell
python -m pytest tests/test_integration.py tests/test_project_paths.py tests/test_vps_preflight.py
```

## เอกสารเพิ่มเติม

- [.env.example](.env.example)
- [docs/WINDOWS_ALWAYS_ON_SETUP_TH.md](docs/WINDOWS_ALWAYS_ON_SETUP_TH.md)
- [docs/DAILY_QUICK_START_TH.md](docs/DAILY_QUICK_START_TH.md)
- [docs/VPS_GO_LIVE_CHECKLIST_TH.md](docs/VPS_GO_LIVE_CHECKLIST_TH.md)
- [docs/VPS_PREFLIGHT_CHECKLIST.md](docs/VPS_PREFLIGHT_CHECKLIST.md)
- [docs/CONFIGURATION_SCHEMA.md](docs/CONFIGURATION_SCHEMA.md)
- [docs/HELD_COINS_TECHNICAL.md](docs/HELD_COINS_TECHNICAL.md)
- [docs/PRODUCTION_DEPLOYMENT_SUMMARY.md](docs/PRODUCTION_DEPLOYMENT_SUMMARY.md)

## ลำดับแนะนำสำหรับการเริ่มต้นใช้งาน

1. สร้าง `.env` ด้วยค่า Bitkub จริงและตั้ง `LIVE_TRADING=false` ไว้ก่อน
2. ติดตั้ง Python dependencies
3. เริ่มจาก `BOT_STARTUP_TEST_MODE=1` แล้วเช็ก health endpoint
4. รัน strict preflight ให้ผ่าน
5. ค่อยตัดสินใจว่าจะเปิด live trading หรือไม่
