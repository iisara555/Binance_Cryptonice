# Crypto Bot V1 (Enterprise-Grade Binance Thailand Trading Runtime)

Crypto Bot V1 คือบอทเทรด Binance Thailand แบบ standalone ที่โฟกัส runtime ฝั่ง terminal เป็นหลัก โดยมีระบบ strategy, risk management, execution, portfolio rebalance, balance monitoring, health endpoint, Telegram alerts และเครื่องมือ preflight สำหรับตรวจความพร้อมก่อนใช้งานจริง

> **หมายเหตุ:** โปรเจกต์ในเวอร์ชันปัจจุบันได้ตัดส่วน Frontend / Dashboard ออกทั้งหมด และปรับสถาปัตยกรรมให้เป็นแบบ Headless & Terminal-First เพื่อความรวดเร็ว เสถียรภาพ และประหยัดทรัพยากรสูงสุด เหมาะสำหรับการรันบน VPS หรือ Windows Server แบบ 24/7

ถ้าต้องการคู่มือสั้นมากสำหรับใช้งานทุกวัน ให้ดู [docs/DAILY_QUICK_START_TH.md](docs/DAILY_QUICK_START_TH.md)

## จุดเด่นของระบบ (Core Features)

*   **Advanced Trading Strategy:** กลยุทธ์ Dual EMA (50/200) + MACD Crossover ตัดสินใจแม่นยำ พร้อมวิเคราะห์แนวโน้มจากหลายกรอบเวลา (Multi-Timeframe Confluence)
*   **Dynamic Risk Management:** คำนวณจุดตัดขาดทุน (SL) และทำกำไร (TP) อัตโนมัติด้วยค่าความผันผวน (ATR) พร้อมระบบเลื่อนจุดตัดขาดทุนเพื่อล็อกกำไร (Trailing Stop)
*   **Position Sizing & Kelly Criterion:** คำนวณขนาดไม้การเทรดแบบ Fractional Kelly ตามความเสี่ยงสูงสุดต่อไม้ (เช่น 1.5% ของพอร์ต) ช่วยปกป้องเงินต้นได้อย่างมีประสิทธิภาพ
*   **Smart Order Management System (OMS):**
    *   ติดตามสถานะออเดอร์แบบ Real-time และแก้ปัญหาออเดอร์ค้าง (Reprice/Cancel) อัตโนมัติ
    *   ทนทานต่อ API ล่มด้วยระบบ Circuit Breaker และ Token Bucket Rate Limiter (ลดความเสี่ยงโดน exchange rate-limit)
    *   กู้คืนสถานะออเดอร์เมื่อไฟดับหรือรีสตาร์ทบอท (DB-First Reconciliation)
*   **Hybrid Dynamic Coin Selection:** สแกนหาคู่เหรียญที่ถือครองอยู่บน Binance Thailand อัตโนมัติ ผสานกับระบบ Whitelist ที่ตั้งค่าเพิ่มเองได้
*   **Rich CLI Dashboard:** หน้าปัดควบคุมผ่าน Terminal ที่สวยงาม แสดงพอร์ตโฟลิโอ สถานะระบบ ออเดอร์ที่เปิดอยู่ พร้อมช่องแชทสำหรับพิมพ์คำสั่ง (Buy/Sell/Close/Track) สดๆ
*   **Complete Observability:** มีระบบแจ้งเตือนเข้า Telegram ทุกการเคลื่อนไหวสำคัญ พร้อม Endpoint ตรวจสุขภาพ (`/health`) และ Endpoint สำหรับ Grafana (`/metrics`)

## โครงสร้างโปรเจกต์

```text
.
|- main.py                          # Entry point หลักของ trading bot
|- bot_config.yaml                  # Runtime configuration หลัก (ปรับแต่งทุกอย่างที่นี่)
|- config.py                        # โหลด Environment Variables เชิงลึก
|- cli_ui.py                        # UI หน้าจอ Dashboard บน Terminal (Rich)
|- trading_bot.py                   # ตัวควบคุมลูปหลักของการเทรด (Orchestrator)
|- trade_executor.py                # ระบบ OMS จัดการยิง/ยกเลิก/ติดตามคำสั่งซื้อขาย
|- signal_generator.py              # ตัวสร้างและรวบรวมสัญญาณการเทรด (Sniper)
|- risk_management.py               # ตัวจัดการความเสี่ยง (Position sizing, R:R)
|- bitkub_websocket.py              # Legacy WebSocket adapter; Binance TH runtime uses REST until a Binance stream adapter exists
|- database.py                      # จัดการ SQLite (เขียนแบบ WAL-mode ป้องกัน DB Lock)
|- run_bot.bat                      # Portable Windows launcher
|- restart_bot.bat                  # Wrapper สำหรับ restart loop
|- activate_env.ps1                 # Portable PowerShell venv activation
|- deploy/windows/                  # Windows NSSM service scripts
|- deploy/systemd/                  # Linux systemd templates
|- scripts/vps_preflight.py         # ตัวตรวจ readiness ของระบบ
|- strategies/                      # Technical trading strategies
|- tests/                           # Pytest suite
\- docs/                            # เอกสารประกอบการใช้งานและ deploy
```

## ความต้องการของระบบ

- Python 3.10 ขึ้นไป (แนะนำเวอร์ชันล่าสุดที่เสถียรจาก [python.org](https://www.python.org/downloads/) หรือ Microsoft Store; ทดสอบกับ 3.14)
- Binance Thailand API key และ secret ที่ถูกต้อง ถ้าจะใช้ private API หรือ live mode
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
BINANCE_API_KEY=your_real_binance_th_key
BINANCE_API_SECRET=your_real_binance_th_secret
LIVE_TRADING=false
LOG_LEVEL=INFO
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

หมายเหตุ:

- `BINANCE_API_KEY` และ `BINANCE_API_SECRET` เป็นค่าบังคับ ถ้าหายหรือเป็น placeholder ระบบจะ fail-fast ตอน startup
- `LIVE_TRADING=false` คือค่าที่ปลอดภัยกว่าในช่วงเริ่มต้น
- `TELEGRAM_BOT_TOKEN` และ `TELEGRAM_CHAT_ID` เป็น optional ถ้าไม่ตั้ง Telegram ระบบยังรันได้

### 2. ไฟล์ `bot_config.yaml`

ก่อนรันระบบ ควรเปิดดูอย่างน้อย section เหล่านี้

- `trading.mode`: `dry_run`, `semi_auto`, `full_auto`
- `data.*`: การเลือก runtime pairs และ hybrid whitelist
- `candle_retention.*`: retention ของ candle ใน SQLite และรอบ auto cleanup กันฐานข้อมูลบวม
- `multi_timeframe.*`: การวิเคราะห์หลาย timeframe
- `risk.*`: daily loss, max position, max open positions
- `rebalance.*`: พฤติกรรม portfolio rebalance
- `monitoring.health_check_host` (ค่าเริ่มต้น `127.0.0.1`), `health_check_port`, `health_check_path`: bot health HTTP endpoint — bind loopback บน VPS เพื่อไม่เปิดพอร์ตสู่สาธารณะ
- `notifications.telegram_command_polling_enabled`: ปิด long-poll command ได้โดยไม่ปิด outbound alerts

ดูรายละเอียด field ทั้งหมดได้ที่ [docs/CONFIGURATION_SCHEMA.md](docs/CONFIGURATION_SCHEMA.md)

### 2.1 Runtime Whitelist (current profile)

runtime นี้ใช้ไฟล์ `coin_whitelist.json` สำหรับกำหนดขอบเขตสินทรัพย์ที่อนุญาตให้ระบบพิจารณา โดยใน schema ปัจจุบันให้ใส่ `base asset` (เช่น `BTC`) ไม่ใช่คู่แบบ `BTCUSDT` หรือ `THB_BTC`

default profile ปัจจุบัน:

- `BTC`
- `ETH`
- `BNB`
- `SOL`
- `XRP`
- `ADA`
- `DOGE`
- `LINK`
- `MATIC`

ดูตัวอย่างไฟล์แบบเต็มและคำอธิบายเพิ่มเติมได้ที่ [docs/MANUAL_THAI.md](docs/MANUAL_THAI.md)

### Candle retention

runtime นี้เก็บ candle ลง SQLite ก่อน แล้วค่อยให้ strategy/MTF อ่านจากฐานข้อมูลท้องถิ่น ดังนั้นจึงมี section `candle_retention` ใน `bot_config.yaml` เพื่อกันตาราง `prices` โตไม่จำกัด

ค่า default ที่ตั้งมาให้คือ:

- `1m`: 7 วัน
- `5m`: 14 วัน
- `15m`: 30 วัน
- `1h`: 60 วัน
- `4h`: 90 วัน
- `1d`: 180 วัน

ระบบจะรัน cleanup ตอน startup และตามรอบ `cleanup_interval_hours` แบบ background-safe โดยจะลบเฉพาะ candle เก่าตาม timeframe เท่านั้น ส่วน `vacuum_after_cleanup` ปิดไว้ default เพราะอาจใช้เวลานานใน runtime จริง

## การติดตั้ง

```powershell
python -m venv .venv
.\activate_env.ps1
pip install -r requirements.txt
```

หมายเหตุ:
คำสั่งและ launcher ของโปรเจกต์จะมองหา `.venv` ก่อน แล้ว fallback ไป `.venv-3` หรือ `venv` เพื่อให้รองรับเครื่องที่ย้ายมาจาก setup เก่าได้

ถ้าต้องการเครื่องมือสำหรับ test และ code quality เพิ่ม:

```powershell
pip install -r requirements-dev.txt
```

## วิธีรัน

### Safe smoke test

ใช้โหมดนี้สำหรับตรวจระบบแบบปลอดภัยที่สุดใน local รอบแรก มันจะบังคับ `dry_run`, `read_only` และปิด Telegram polling ให้

```powershell
$env:BOT_STARTUP_TEST_MODE = "1"
.\.venv\Scripts\python.exe main.py
```

### Standalone bot

รัน bot จาก root ของโปรเจกต์ด้วย launcher ที่ portable ที่สุด:

```powershell
.\run_bot.bat
```

ถ้าต้องการเรียก Python โดยตรง:

```powershell
.\.venv\Scripts\python.exe main.py
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

repo นี้มี 2 ไฟล์สำหรับ VPS runtime ที่เก็บ Rich CLI ไว้ได้แม้หลัง reboot:

- [deploy/systemd/crypto-bot-tmux.sh](deploy/systemd/crypto-bot-tmux.sh)
- [deploy/systemd/crypto-bot-tmux.service](deploy/systemd/crypto-bot-tmux.service)

แนวทางนี้ให้ `systemd` ทำหน้าที่ auto-start ตอน boot แล้วสร้าง `tmux` session ชื่อ `crypto` ขึ้นมาแทนการรัน bot ตรง ๆ ทำให้ attach กลับไปดู Rich CLI ได้ภายหลัง

ตัวอย่างติดตั้งบน VPS:

```bash
cd /opt
git clone <your-repo-url> crypto-bot-v1
cd /opt/crypto-bot-v1
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
chmod +x deploy/systemd/crypto-bot-tmux.sh
sudo cp deploy/systemd/crypto-bot-tmux.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable crypto-bot-tmux
sudo systemctl start crypto-bot-tmux
tmux attach -t crypto
```

คำสั่งที่ใช้บ่อยบน VPS:

```bash
systemctl status crypto-bot-tmux --no-pager -l
tmux list-sessions
tmux attach -t crypto
curl http://127.0.0.1:8080/health
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

- เกิดขึ้นเมื่อ Binance Thailand private API ใช้งานไม่ได้ เช่น credentials หรือ permissions ไม่ถูกต้อง
- bot จะยังรันอยู่ใน public-only safe mode
- health endpoint จะรายงาน `status=degraded`
- ห้ามตีความว่าเป็นพร้อม live trading

## Health endpoint

### Bot health

- `GET http://127.0.0.1:8080/health`

endpoint นี้อ้างอิงค่าจาก `monitoring.health_check_port` และ `monitoring.health_check_path` ใน `bot_config.yaml`

ใช้ดู:

- โหมดการทำงานและความพร้อมของ bot
- สถานะ auth-degraded ถ้า Binance Thailand private API ใช้ไม่ได้
- state ของ runtime loop, risk summary และ service health

## Preflight checks

รัน strict readiness check:

```powershell
.\.venv\Scripts\python.exe scripts/vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --json
```

ถ้าคุณตั้งใจตรวจระบบใน public-only degraded mode ชั่วคราว:

```powershell
.\.venv\Scripts\python.exe scripts/vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --allow-auth-degraded --json
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

1. สร้าง `.env` ด้วยค่า Binance Thailand จริงและตั้ง `LIVE_TRADING=false` ไว้ก่อน
2. ติดตั้ง Python dependencies
3. เริ่มจาก `BOT_STARTUP_TEST_MODE=1` แล้วเช็ก health endpoint
4. รัน strict preflight ให้ผ่าน
5. ค่อยตัดสินใจว่าจะเปิด live trading หรือไม่
