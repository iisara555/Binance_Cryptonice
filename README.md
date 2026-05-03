# Binance Cryptonice

**Binance Cryptonice** คือ automated trading bot สำหรับ Binance (USDT spot pairs) แบบ standalone ที่รันบน Terminal เป็นหลัก มีระบบ dual-strategy engine, risk management, OMS, portfolio monitoring, health endpoint, Telegram alerts และเครื่องมือ preflight สำหรับตรวจความพร้อมก่อน go-live

> โปรเจกต์ออกแบบมาเป็น **Terminal-First / Headless-capable** — รันได้ทั้งบน Windows (NSSM service) และ Linux VPS (tmux + systemd) แบบ 24/7

ถ้าต้องการคู่มือสั้นสำหรับใช้งานทุกวัน ดูที่ [docs/DAILY_QUICK_START_TH.md](docs/DAILY_QUICK_START_TH.md)

## จุดเด่นของระบบ (Core Features)

- **Dual Strategy Engine:** รัน `machete_v8b_lite` + `simple_scalp_plus` พร้อมกัน ใน scalping mode (15m primary timeframe); OR-logic — แต่ละ strategy ตัดสินใจได้อิสระ
- **Multi-Timeframe Confluence (MTF):** ยืนยัน signal จาก 1m / 5m / 15m / 1h / 4h / 1d ก่อนเปิด position จริง
- **Dynamic Risk Management:** SL/TP อิง ATR อัตโนมัติ (scalping: SL 1% / TP 3%, R:R ≈ 1:3), Trailing Stop สำหรับล็อคกำไร, daily loss cap
- **Position Sizing (Fractional Kelly):** คำนวณขนาดออเดอร์อิงความเสี่ยงต่อไม้ (2.5% ของพอร์ต) พร้อม hard-cap ต่อ position (28%)
- **Smart OMS:** ติดตามสถานะ order real-time, Reprice/Cancel อัตโนมัติ, reconcile กับ Binance ตอน startup + periodic
- **Circuit Breaker + Rate Limiter:** ทนต่อ API ล่ม — Token Bucket Rate Limiter ป้องกัน rate-limit, Circuit Breaker ป้องกัน cascade failure
- **Hybrid Dynamic Coin Selection:** ดึง pair จากเหรียญที่ถือไว้ใน Binance ผสาน whitelist ที่ตั้งเองได้
- **Bloomberg-style Rich CLI Dashboard:** TUI panel แสดง Positions, SigFlow, Risk Rails, Balance, System Status, Log Stream แบบ real-time
- **Complete Observability:** Telegram alerts ทุก event สำคัญ, `/health` endpoint, `/metrics` endpoint (Prometheus-compatible)

## โครงสร้างโปรเจกต์

```text
.
├── main.py                          # TradingBotApp — entrypoint, โหลด config / collector / orchestrator
├── bot_config.yaml                  # Runtime configuration หลัก
├── config.py                        # Environment + validation
├── cli_ui.py                        # Rich terminal dashboard (Bloomberg-style TUI)
├── trading_bot.py                   # TradingBotOrchestrator (facade — logic เชิงลึกใต้ trading/)
├── trade_executor.py                # OMS — คำสั่งซื้อขาย ยกเลิก reconcile
├── signal_generator.py              # Signal pipeline / strategy dispatch
├── risk_management.py               # Risk, SL/TP, pre-trade gates
├── dynamic_coin_config.py           # Hybrid pair selection (holdings + whitelist)
├── coin_whitelist.json              # USDT whitelist assets (BTC/ETH/SOL/DOGE/ADA/…)
├── database.py                      # SQLite WAL — candle + trade persistence
├── binance_websocket.py             # Binance WebSocket adapter (live price feed)
├── balance_monitor.py               # Balance monitor (Binance REST)
├── monitoring.py                    # Health endpoint + metrics + order reconcile
├── indicators.py                    # EMA, MACD, RSI, Bollinger, ATR
├── portfolio_manager.py             # Portfolio valuation + rebalance
├── signal_pipeline.py               # Pre/post-filter signal pipeline
├── logger_setup.py                  # Structured log setup (Rich console + file)
├── alerts.py / telegram_bot.py      # Telegram alert + command polling
├── health_server.py                 # /health + /metrics HTTP server
├── trading/
│   ├── orchestrator.py              # BotMode, TradeDecision, SignalSource
│   ├── signal_runtime.py            # Per-pair iteration, execution plan, guards
│   ├── execution_runtime.py         # Semi/full/dry, pending approvals
│   ├── startup_runtime.py           # Reconcile on startup + bootstrap
│   ├── portfolio_runtime.py         # Portfolio snapshots / marks
│   ├── position_monitor.py          # Trailing stop + SL/TP monitoring
│   ├── managed_lifecycle.py         # Managed component lifecycle
│   ├── bootstrap_config.py          # Dynamic config bootstrap (whitelist → pairs)
│   ├── cli_snapshot_builder.py      # Dashboard snapshot assembly
│   └── bot_runtime/                 # BotRuntime delegates (loop, WS, pause, …)
├── strategies/                      # machete_v8b_lite.py, simple_scalp_plus.py
├── scripts/
│   ├── vps_preflight.py             # Pre-deploy readiness check
│   └── deploy_vps_runtime.ps1       # One-shot VPS deploy (SCP + restart + health)
├── deploy/
│   ├── systemd/                     # crypto-bot-tmux.service + crypto-bot-tmux.sh
│   └── windows/                     # NSSM service installer / health monitor
├── tests/                           # pytest test suite
└── docs/                            # Full documentation index
```

ดัชนีเอกสาร: [docs/README.md](docs/README.md) — [docs/ADR-001-domain-boundaries-and-dependencies.md](docs/ADR-001-domain-boundaries-and-dependencies.md)

## ความต้องการของระบบ

- Python 3.10+ (ทดสอบบน 3.11/3.12/3.14)
- Binance API Key + Secret (read + spot trading permissions)
- PowerShell 5+ บน Windows หรือ bash บน Linux/VPS
- NSSM (`nssm.exe`) และสิทธิ Administrator ถ้าจะใช้ Windows service mode

## การตั้งค่า

### 1. ไฟล์ `.env` ที่ root โปรเจกต์

เริ่มจากคัดลอก `.env.example` ไปเป็น `.env`

```powershell
Copy-Item .env.example .env
```

จากนั้นแก้ค่าใน `.env` ให้เป็นของจริง

```env
BINANCE_API_KEY=your_binance_api_key
BINANCE_API_SECRET=your_binance_api_secret
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

ก่อนรันระบบ ควรตรวจสอบ section เหล่านี้:

- `trading.mode`: `dry_run` / `semi_auto` / `full_auto` (default: `full_auto`)
- `strategy_mode.active`: `scalping` (default) — SL 1% / TP 3%, primary TF 15m
- `mode_indicator_profiles.scalping.active_strategies`: `["machete_v8b_lite", "simple_scalp_plus"]`
- `data.*`: runtime pairs และ hybrid whitelist settings
- `candle_retention.*`: retention policy ต่อ timeframe (1m=7d, 5m=14d, 15m=30d, 1h=60d)
- `multi_timeframe.*`: MTF confirmation (1m/5m/15m required, 1h/4h/1d optional)
- `risk.*`: daily loss cap, max open positions (default 6), max trades/day (default 8)
- `monitoring.health_check_host` / `health_check_port` / `health_check_path`: bind loopback บน VPS
- `notifications.telegram_command_polling_enabled`: ปิด command polling ได้โดยไม่ปิด outbound alerts

ดูรายละเอียด field ทั้งหมดที่ [docs/CONFIGURATION_SCHEMA.md](docs/CONFIGURATION_SCHEMA.md)

### 2.1 Runtime Whitelist (current profile)

ไฟล์ `coin_whitelist.json` กำหนด base assets ที่อนุญาตให้ระบบพิจารณา (ใส่เป็น base เช่น `BTC` ไม่ใช่ `BTCUSDT`)

ค่า default ปัจจุบัน (USDT pairs):

- `BTC`, `ETH`, `BNB`, `SOL`, `XRP`, `ADA`, `DOGE`, `LINK`, `POL`

แก้ whitelist ที่ไฟล์ [coin_whitelist.json](coin_whitelist.json) — ระบบจะ resolve เป็น `{BASE}USDT` อัตโนมัติ

**หมายเหตุ:** `include_assets_with_balance: true` หมายความว่าระบบจะรวมเหรียญที่มี balance ใน Binance เข้า active pairs ด้วย แม้ไม่ได้อยู่ใน whitelist

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

ถ้าเคยเปิด `crypto-sniper.service` / `crypto-bot-runtime.service` คู่กับ tmux ให้สลับให้เหลือแค่ tmux แบบถาวรด้วยคำสั่งเดียวบน VPS: `sudo bash deploy/systemd/vps_switch_to_tmux_only.sh` จากนั้นเข้า dashboard ด้วย `crypto-tmux` (symlink ไปที่ `deploy/systemd/crypto-attach`) หรือ `tmux attach -t crypto`

ตัวอย่างติดตั้งบน VPS:

```bash
cd /root
git clone https://github.com/iisara555/Binance_Cryptonice Crypto_Sniper
cd /root/Crypto_Sniper
python3.11 -m venv .venv
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

## Trading Parameters (Scalping Mode)

| Parameter | Value |
|---|---|
| Active Strategies | `machete_v8b_lite` + `simple_scalp_plus` |
| Primary Timeframe | 15m |
| Stop Loss | 1.0% |
| Take Profit | 3.0% (R:R ≈ 1:3) |
| ATR Multiplier | 1.5× |
| Min Confidence | 0.40 |
| Max Trades/Day | 8 |
| Max Open Positions | 6 |
| Position Size Cap | 28% ของพอร์ต |
| Risk Per Trade | 2.5% |
| Min Time Between Trades | 15 นาที |

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

## VPS Deploy (One-shot)

ถ้ามีไฟล์ `scripts/deploy_vps_runtime.ps1` สามารถ deploy runtime ไปยัง VPS ได้ด้วยคำสั่งเดียว (SCP → restart service → health check):

```powershell
powershell -ExecutionPolicy RemoteSigned -File scripts/deploy_vps_runtime.ps1
```

script จะสร้าง pre-deploy backup ก่อน upload, restart `crypto-bot-tmux.service` และรอยืนยัน `/health` ก่อนรายงาน success

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

- [docs/README.md](docs/README.md) — **ดัชนีเอกสารทั้งหมด** (เริ่มที่นี่)
- [docs/ADR-001-domain-boundaries-and-dependencies.md](docs/ADR-001-domain-boundaries-and-dependencies.md) — โดเมน, `trading/` module map
- [docs/CONFIGURATION_SCHEMA.md](docs/CONFIGURATION_SCHEMA.md) — คำอธิบาย field ทุกตัวใน `bot_config.yaml`
- [docs/DAILY_QUICK_START_TH.md](docs/DAILY_QUICK_START_TH.md) — คู่มือรายวัน (ภาษาไทย)
- [docs/WINDOWS_ALWAYS_ON_SETUP_TH.md](docs/WINDOWS_ALWAYS_ON_SETUP_TH.md) — Windows service setup
- [docs/VPS_GO_LIVE_CHECKLIST_TH.md](docs/VPS_GO_LIVE_CHECKLIST_TH.md) — VPS go-live checklist
- [docs/VPS_PREFLIGHT_CHECKLIST.md](docs/VPS_PREFLIGHT_CHECKLIST.md) — Preflight checks
- [docs/SECURITY_AUDIT_REPORT.md](docs/SECURITY_AUDIT_REPORT.md) — Code security audit
- [.env.example](.env.example) — Environment variable template
- [docs/CONFIGURATION_SCHEMA.md](docs/CONFIGURATION_SCHEMA.md)
- [docs/HELD_COINS_TECHNICAL.md](docs/HELD_COINS_TECHNICAL.md)
- [docs/PRODUCTION_DEPLOYMENT_SUMMARY.md](docs/PRODUCTION_DEPLOYMENT_SUMMARY.md)

## ลำดับแนะนำสำหรับการเริ่มต้นใช้งาน

1. สร้าง `.env` ด้วยค่า Binance Thailand จริงและตั้ง `LIVE_TRADING=false` ไว้ก่อน
2. ติดตั้ง Python dependencies
3. เริ่มจาก `BOT_STARTUP_TEST_MODE=1` แล้วเช็ก health endpoint
4. รัน strict preflight ให้ผ่าน
5. ค่อยตัดสินใจว่าจะเปิด live trading หรือไม่

