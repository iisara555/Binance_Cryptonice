# 📘 Crypto Sniper Bot - คู่มือฉบับสมบูรณ์

> **เวอร์ชัน:** 1.0  
> **อัปเดตล่าสุด:** 14 เมษายน 2569  
> **สถานะ:** ✅ พร้อมใช้งานจริง (ยืนยันรอบ final verification แล้ว)

---

## 📑 สารบัญ

0. [บทนำ - จาก README](#0-บทนำ-จาก-readme)
1. [ภาพรวมระบบ](#1-ภาพรวมระบบ)
2. [การติดตั้งและเริ่มต้น](#2-การติดตั้งและเริ่มต้น)
3. [การตั้งค่าคอนฟิก](#3-การตั้งค่าคอนฟิก)
4. [โหมดการใช้งาน](#4-โหมดการใช้งาน)
5. [Held Coins Only Mode](#5-held-coins-only-mode)
6. [การ Deploy บน VPS](#6-การ-deploy-บน-vps)
7. [การตั้งค่า Windows Always-On](#7-การตั้งค่า-windows-always-on)
8. [การย้ายระบบไปเครื่องใหม่](#8-การย้ายระบบไปเครื่องใหม่)
9. [การดูแลรักษาและแก้ไขปัญหา](#9-การดูแลรักษาและแก้ไขปัญหา)
10. [รายงานการตรวจสอบล่าสุด](#10-รายงานการตรวจสอบล่าสุด)

---

# 0. บทนำ - จาก README

## 0.1 ความเป็นมา

**Crypto Bot V1** คือบอทเทรด Bitkub แบบ standalone ที่โฟกัส runtime ฝั่ง terminal เป็นหลัก โดยมีระบบ strategy, risk management, execution, portfolio rebalance, balance monitoring, health endpoint, Telegram alerts และเครื่องมือ preflight สำหรับตรวจความพร้อมก่อนใช้งานจริง

> **หมายเหตุ:** โปรเจกต์ในเวอร์ชันปัจจุบันได้ตัดส่วน Frontend / Dashboard ออกทั้งหมด และปรับสถาปัตยกรรมให้เป็นแบบ Headless & Terminal-First เพื่อความรวดเร็ว เสถียรภาพ และประหยัดทรัพยากรสูงสุด เหมาะสำหรับการรันบน VPS หรือ Windows Server แบบ 24/7

## 0.2 จุดเด่นของระบบ (Core Features)

| ความสามารถ | รายละเอียด |
|------------|------------|
| **Advanced Trading Strategy** | กลยุทธ์ Dual EMA (50/200) + MACD Crossover ตัดสินใจแม่นยำ พร้อมวิเคราะห์แนวโน้มจากหลายกรอบเวลา (Multi-Timeframe Confluence) |
| **Dynamic Risk Management** | คำนวณ SL/TP อัตโนมัติด้วย ATR พร้อมระบบ Trailing Stop |
| **Position Sizing & Kelly Criterion** | คำนวณขนาดไม้การเทรดแบบ Fractional Kelly ตามความเสี่ยงสูงสุดต่อไม้ (เช่น 1.5% ของพอร์ต) |
| **Smart OMS** | ติดตามออเดอร์ real-time, ระบบ Circuit Breaker, Token Bucket Rate Limiter |
| **Hybrid Dynamic Coin Selection** | สแกนหาคู่เหรียญที่ถือครองอยู่บนกระดาน Bitkub อัตโนมัติ |
| **Rich CLI Dashboard** | แสดงพอร์ตโฟลิโอ สถานะระบบ ออเดอร์ที่เปิดอยู่ พร้อมช่องแชทสำหรับพิมพ์คำสั่ง |
| **Complete Observability** | แจ้งเตือน Telegram ทุกการเคลื่อนไหว พร้อม `/health` และ `/metrics` endpoints |

## 0.3 โครงสร้างโปรเจกต์

> **หมายเหตุ:** runtime ปัจจุบันเน้น **Binance Thailand** (headless / terminal-first) พร้อม fallback adapter เก่า — โครงด้านล่างสะท้อน repo ปัจจุบัน ไม่ใช่ชื่อโฟลเดอร์เดียวกับ production ของคุณก็ได้

```
<project-root>/
├── main.py                          # TradingBotApp — entry หลัก
├── bot_config.yaml                  # Configuration หลัก
├── config.py                         # ENV + validation
├── cli_ui.py                         # Rich CLI / dashboard
├── trading_bot.py                    # TradingBotOrchestrator (~1450 LOC — facade)
├── trade_executor.py                 # OMS
├── signal_generator.py               # Signal / strategies glue
├── risk_management.py               # Risk & gates
├── api_client.py                    # Binance TH client
├── database.py                      # SQLite WAL
├── binance_websocket.py             # WS ราคา (เมื่อมี)
├── trading/
│   ├── orchestrator.py              # BotMode, TradeDecision, …
│   ├── bot_runtime/                 # Logic ที่ delegate จาก orchestrator (ลูป, WS, pause, iteration, …)
│   ├── signal_runtime.py
│   ├── execution_runtime.py
│   ├── portfolio_runtime.py
│   └── …                            # startup, retention, monitor, …
├── strategies/
├── deploy/ (windows/, systemd/, …)
├── scripts/
├── docs/                            # เริ่มที่ docs/README.md
└── tests/
```

ดู **ดัชนีเอกสารครบถ้วน**: [README.md](./README.md) และ **ADR สถาปัตยกรรม**: [ADR-001-domain-boundaries-and-dependencies.md](./ADR-001-domain-boundaries-and-dependencies.md)

## 0.4 ความต้องการของระบบ

| รายการ | รายละเอียด |
|--------|------------|
| Python | 3.10+ |
| Bitkub API | API Key + Secret + IP Allowlist |
| Windows | PowerShell 5.1+ (สำหรับ Windows) |
| Linux/VPS | systemd + tmux (สำหรับ VPS) |

## 0.5 ลำดับการเริ่มต้นใช้งาน

```
1. สร้าง .env ด้วยค่า Bitkub จริง + ตั้ง LIVE_TRADING=false ไว้ก่อน
2. ติดตั้ง Python dependencies
3. เริ่มจาก BOT_STARTUP_TEST_MODE=1 แล้วเช็ก health endpoint
4. รัน strict preflight ให้ผ่าน
5. ค่อยตัดสินใจว่าจะเปิด live trading หรือไม่
```

# 1. ภาพรวมระบบ

## 1.1 ระบบทำงานยังไง

Crypto Sniper Bot เป็นระบบเทรดอัตโนมัติที่ทำงานบน Bitkub โดยมี flow หลักดังนี้:

```
┌─────────────────────────────────────────────────────────────────┐
│                      วงจรการทำงานของ Bot                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   ┌──────────┐    ┌──────────────┐    ┌─────────────┐           │
│   │ Bitkub   │───▶│ Signal       │───▶│ Risk        │           │
│   │ WebSocket│    │ Generator    │    │ Manager     │           │
│   └──────────┘    └──────────────┘    └─────────────┘           │
│                                            │                     │
│                                            ▼                     │
│   ┌──────────┐    ┌──────────────┐    ┌─────────────┐           │
│   │ Monitor  │◀───│ State        │◀───│ Trade       │           │
│   │ Service  │    │ Management   │    │ Executor    │           │
│   └──────────┘    └──────────────┘    └─────────────┘           │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## 1.2 ความสามารถหลัก

| ความสามารถ | รายละเอียด |
|------------|------------|
| **Signal Generation** | ใช้ 4 strategies (trend_following, mean_reversion, breakout, scalping) |
| **Multi-Timeframe** | วิเคราะห์ข้อมูลหลาย timeframe พร้อมกัน |
| **Risk Management** | คำนวณ SL/TP อัตโนมัติ, จำกัดความเสี่ยงต่อวัน |
| **Position Tracking** | ติดตาม positions, trailing stops, exit triggers |
| **Portfolio Rebalancing** | ปรับสมดุลพอร์ตอัตโนมัติตาม target allocation |
| **Balance Monitoring** | เฝ้าดู deposit/withdrawal และแจ้งเตือน |
| **Telegram Alerts** | แจ้งสถานะ trade ผ่าน Telegram |
| **Rich Terminal UI** | แสดง dashboard สวยงามใน terminal |

## 1.3 โครงสร้างไฟล์ (รากโปรเจกต์ + trading)

```
<project-root>/
├── main.py
├── trading_bot.py              # TradingBotOrchestrator (~1450 LOC — facade)
├── trade_executor.py
├── signal_generator.py
├── risk_management.py
├── state_management.py
├── api_client.py
├── balance_monitor.py
├── cli_ui.py
├── monitoring.py
├── binance_websocket.py        # หรือไฟล์ WS อื่นตามที่ติดตั้ง
├── trading/
│   ├── orchestrator.py         # BotMode, TradeDecision…
│   ├── bot_runtime/            # ลูปหลัก, WS, iteration, pause, deps…
│   ├── signal_runtime.py
│   ├── execution_runtime.py
│   ├── portfolio_runtime.py
│   ├── position_manager.py
│   └── …
├── strategies/
│   ├── trend_following.py
│   ├── mean_reversion.py
│   ├── breakout.py
│   └── scalping.py
├── docs/
├── scripts/
├── deploy/
└── tests/
```

---

# 2. การติดตั้งและเริ่มต้น

## 2.1 สิ่งที่ต้องมี

| รายการ | รายละเอียด |
|--------|------------|
| Python | 3.10 ขึ้นไป |
| Bitkub API | API Key + Secret |
| Bitkub IP Allowlist | เพิ่ม IP ของเครื่องที่จะรัน |
| Windows PowerShell | 5.1+ (สำหรับ Windows) |
| หรือ Linux/VPS | พร้อม systemd |

## 2.2 การติดตั้ง

```powershell
# 1. Clone หรือ copy โปรเจกต์
cd "C:\Users\YourUser\Desktop\Crypto_Sniper"

# 2. สร้าง Virtual Environment
python -m venv .venv-3

# 3. Activate
.\activate_env.ps1

# 4. ติดตั้ง dependencies
pip install -r requirements.txt

# 5. สร้าง .env จาก example
copy .env.example .env
```

## 2.3 ตั้งค่า .env

```env
# Bitkub API Credentials
BITKUB_API_KEY=your_api_key_here
BITKUB_API_SECRET=your_api_secret_here

# Trading Mode
LIVE_TRADING=false        # true = จริง, false = ทดสอบ
BOT_READ_ONLY=false       # true = อ่านอย่างเดียว ไม่เทรด

# Logging
LOG_LEVEL=INFO

# Telegram (ถ้ามี)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

## 2.4 การเริ่มต้นแบบปลอดภัย

```powershell
# ทดสอบก่อนใช้จริง
Set-Location "C:\path\to\Crypto_Sniper"
$env:BOT_STARTUP_TEST_MODE = "1"
.\.venv-3\Scripts\python.exe main.py
```

## 2.5 การตรวจสอบ Health

```powershell
# ดูสถานะ bot
Invoke-RestMethod http://127.0.0.1:8080/health

# รัน Preflight Script
.\.venv-3\Scripts\python.exe scripts/vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --json
```

**ผลลัพธ์ที่ดี:**
```json
{
  "status": "pass",
  "healthy": true,
  "details": {...}
}
```

**ถ้า degraded (ยังไม่พร้อม):**
```json
{
  "status": "degraded",
  "healthy": false
}
```

---

# 3. การตั้งค่าคอนฟิก

## 3.1 bot_config.yaml โครงสร้าง

```yaml
# === Trading Mode ===
trading:
  trading_pair: ""           # คู่เทรดหลัก (ว่าง = ใช้ holdings)
  interval_seconds: 60       # ช่วงเวลาระหว่าง loop
  timeframe: "15m"           # Timeframe หลัก
  mode: "dry_run"            # dry_run, semi_auto, full_auto

# === Strategies ===
strategies:
  enabled:
    - trend_following
    - mean_reversion
    - breakout
    - scalping
  min_confidence: 0.35       # ความมั่นใจขั้นต่ำ
  min_strategies_agree: 2    # ต้องกี่ strategies ตรงกัน

# === Risk Management ===
risk:
  max_risk_per_trade_pct: 4.0      # ความเสี่ยงสูงสุดต่อ trade (%)
  max_daily_loss_pct: 10.0         # ขาดทุนสูงสุดต่อวัน (%)
  max_position_per_trade_pct: 10.0 # ขนาด position สูงสุด (%)
  max_open_positions: 4            # จำนวน positions เปิดสูงสุด

# === Multi-Timeframe ===
multi_timeframe:
  enabled: true
  require_htf_confirmation: true

# === Notifications ===
notifications:
  alert_channel: "telegram"
  telegram_command_polling_enabled: false
  send_alerts: true

# === Monitoring ===
monitoring:
  enabled: true
  health_check_port: 8080
  health_check_path: "/health"

# === Rebalancing ===
rebalance:
  enabled: true
  strategy: "threshold"
  target_allocation:
    THB: 20.0
    BTC: 40.0
    ETH: 40.0

# === Portfolio Guard ===
data:
  auto_detect_held_pairs: true
  pairs: []
  portfolio_guard:
    held_coins_only: true
  hybrid_dynamic_coin_config:
    whitelist_json_path: "coin_whitelist.json"
    include_assets_with_balance: true
    min_quote_balance_thb: 100.0

# === Candle Retention ===
candle_retention:
  enabled: true
  run_on_startup: true
  vacuum_after_cleanup: false
  cleanup_interval_hours: 12
  timeframes:
    "1m": 7
    "5m": 14
    "15m": 30
    "1h": 60
```

## 3.2 coin_whitelist.json

```json
{
  "version": 1,
  "quote_asset": "THB",
  "min_quote_balance_thb": 100.0,
  "require_supported_market": true,
  "include_assets_with_balance": true,
  "assets": [
    { "symbol": "BTC", "enabled": true },
    { "symbol": "ETH", "enabled": true },
    { "symbol": "BNB", "enabled": true },
    { "symbol": "SOL", "enabled": true },
    { "symbol": "XRP", "enabled": true },
    { "symbol": "ADA", "enabled": true },
    { "symbol": "DOGE", "enabled": true },
    { "symbol": "LINK", "enabled": true },
    { "symbol": "POL", "enabled": true }
  ],
  "updated_at": "2026-04-26T20:00:00+07:00"
}
```

> หมายเหตุ: ในไฟล์นี้ต้องใช้ `base symbol` เช่น `BTC`, `ETH` (ไม่ใช่ `BTCUSDT`).

## 3.3 Recommended Whitelist Notes (2026-04)

reference profile ด้านล่างใช้เพื่อเลือกโหมดให้เหมาะกับสินทรัพย์:

| Symbol | Name | Liquidity | Volatility | Suitable modes | Reason |
|--------|------|-----------|------------|----------------|--------|
| BTCUSDT | Bitcoin | high | medium | scalping, trend_only, standard | สภาพคล่องสูงสุด — อ้างอิงตลาดทั้งหมด |
| ETHUSDT | Ethereum | high | medium-high | scalping, trend_only, standard | สภาพคล่องอันดับ 2 — ecosystem ใหญ่ |
| BNBUSDT | BNB | high | medium | scalping, trend_only, standard | Native token Binance.th — สภาพคล่องดีมากบน platform นี้ |
| SOLUSDT | Solana | high | high | scalping, trend_only | Volume สูง — volatile ดีสำหรับ scalping |
| XRPUSDT | XRP | high | medium-high | scalping, trend_only, standard | Volume สูงมาก — spread แคบ เหมาะ scalping |
| ADAUSDT | Cardano | medium-high | high | scalping, standard | Volume ดี — price ต่ำ position size ยืดหยุ่น |
| DOGEUSDT | Dogecoin | high | very-high | scalping | Volume สูงมาก — meme momentum เหมาะ scalping |
| LINKUSDT | Chainlink | medium-high | high | trend_only, standard | Volume ดี — trend ชัดเจน เหมาะ trend_only |
| POLUSDT | Polygon (POL) | medium-high | high | scalping, standard | Volume ดี — price ต่ำ เหมาะ scalping |

> mapping สำหรับ runtime ไฟล์ `coin_whitelist.json`:
> `BTCUSDT->BTC`, `ETHUSDT->ETH`, `BNBUSDT->BNB`, `SOLUSDT->SOL`, `XRPUSDT->XRP`, `ADAUSDT->ADA`, `DOGEUSDT->DOGE`, `LINKUSDT->LINK`, `POLUSDT->POL`

---

# 4. โหมดการใช้งาน

## 4.1 โหมดที่มี

| โหมด | ค่าใน config | พฤติกรรม |
|------|--------------|----------|
| **Dry Run** | `dry_run` | คำนวณ signals แต่ไม่ execute orders |
| **Semi Auto** | `semi_auto` | ส่ง signal ให้ user ยืนยันก่อน execute |
| **Full Auto** | `full_auto` | execute อัตโนมัติทันที |

## 4.2 วิธีเปลี่ยนโหมด

**ผ่าน bot_config.yaml:**
```yaml
trading:
  mode: "full_auto"  # เปลี่ยนตรงนี้
```

**ผ่าน Environment Variable:**
```env
LIVE_TRADING=true
```

## 4.3 การใช้งานจริง

```
1. ทดสอบใน dry_run จนมั่นใจ
2. ตรวจ health endpoint ให้ healthy: true
3. รัน preflight ให้ pass
4. เปลี่ยนเป็น full_auto
5. ดู Rich terminal dashboard
```

---

# 5. Held Coins Only Mode

## 5.1 คืออะไร

โหมดที่ให้ bot จัดการเฉพาะเหรียญที่คุณถืออยู่จริง ไม่เปิด position ใหม่ในเหรียญที่ไม่ได้ตั้งใจ

## 5.2 ทำไมต้องใช้

- ✅ ลดโอกาสเปิด position ผิดเหรียญ
- ✅ rebalance อยู่ในกรอบพอร์ตจริง
- ✅ เหมาะกับย้ายจาก manual มา bot-assisted

## 5.3 วิธีเปิดใช้

```yaml
data:
  portfolio_guard:
    held_coins_only: true
  hybrid_dynamic_coin_config:
    whitelist_json_path: "coin_whitelist.json"
    include_assets_with_balance: true
```

## 5.4 สิ่งที่จะเกิดขึ้น

| สถานการณ์ | พฤติกรรม |
|-----------|----------|
| เหรียญที่ถืออยู่ได้ managed exit | ✅ อนุญาต |
| เหรียญที่ถืออยู่ถูก rebalance | ✅ อนุญาต |
| เหรียญใหม่นอก whitelist | ❌ ถูก filter |
| whitelist เปลี่ยนตอน runtime | 🔄 hot reload ได้ |

## 5.5 การตรวจสอบว่าใช้งานถูกต้อง

1. ดู Rich terminal `Portfolio Breakdown`
2. ดู logs หา pair resolution
3. ดู health endpoint
4. รัน preflight script

---

# 6. การ Deploy บน VPS

## 6.1 ขั้นตอนเตรียม VPS

### 6.1.1 ติดตั้ง Python และ Dependencies

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install python3 python3-venv python3-pip git

# Clone โปรเจกต์
git clone https://github.com/your-repo/Crypto_Sniper.git
cd Crypto_Sniper

# สร้าง venv
python3 -m venv .venv-3
source .venv-3/bin/activate

# ติดตั้ง dependencies
pip install -r requirements.txt
```

### 6.1.2 ตั้งค่า .env

```bash
cp .env.example .env
nano .env  # ใส่ API key จริง
```

### 6.1.3 เพิ่ม IP ใน Bitkub Allowlist

เพิ่ม public IP ของ VPS ใน Bitkub API settings

## 6.2 ติดตั้ง Linux Service (systemd + tmux)

### 6.2.1 Copy service files

```bash
cp deploy/systemd/crypto-bot-tmux.service /etc/systemd/system/
cp deploy/systemd/crypto-bot-tmux.sh /opt/crypto-bot/
chmod +x /opt/crypto-bot/crypto-bot-tmux.sh
```

### 6.2.2 แก้ไข paths ใน script

```bash
nano /opt/crypto-bot/crypto-bot-tmux.sh
# เปลี่ยน PATH_TO_PROJECT เป็น path จริง
```

### 6.2.3 Enable และ Start

```bash
sudo systemctl daemon-reload
sudo systemctl enable crypto-bot-tmux
sudo systemctl start crypto-bot-tmux
```

## 6.3 คำสั่งที่ใช้บ่อย

```bash
# ดูสถานะ service
sudo systemctl status crypto-bot-tmux

# ดู logs
sudo journalctl -u crypto-bot-tmux -n 100 --no-pager

# Restart
sudo systemctl restart crypto-bot-tmux

# เข้า Rich terminal
tmux list-sessions
tmux attach -t crypto

# ตรวจ health
curl http://127.0.0.1:8080/health

# รัน preflight
python scripts/vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --json
```

## 6.4 VPS Go-Live Checklist

### ก่อน Start Service

- [ ] VPS public IP อยู่ใน Bitkub API allowlist แล้ว
- [ ] `.env` มี key จริงครบ
- [ ] `LIVE_TRADING` เป็นค่าที่ตั้งใจ
- [ ] `bot_config.yaml` ถูกต้อง
- [ ] ใช้ `crypto-bot-tmux.service` + `tmux` session

### หลัง Start Service

- [ ] `systemctl status` ขึ้น `active (exited)`
- [ ] `tmux list-sessions` เห็น session `crypto`
- [ ] `tmux attach -t crypto` เข้า Rich CLI ได้
- [ ] `curl http://127.0.0.1:8080/health` ได้ `healthy: true`
- [ ] health ไม่ขึ้น `status: degraded`

### ก่อนเปิด Live จริง

- [ ] strict preflight ผ่าน
- [ ] Telegram ทำงานตามต้องการ
- [ ] ไม่มี errors ใน logs

---

# 7. การตั้งค่า Windows Always-On

## 7.1 สิ่งที่ต้องมี

- Windows PowerShell 5.1+
- Python venv (`.venv-3`)
- NSSM (`nssm.exe`)
- สิทธิ Administrator

## 7.2 ติดตั้ง NSSM

```powershell
# แตกไฟล์ไปที่
C:\nssm\win64\nssm.exe

# หรือใช้ winget
winget install --id NSSM.NSSM -e
```

## 7.3 ติดตั้ง Service

```powershell
Set-Location "C:\path\to\Crypto_Sniper"

# ติดตั้งทั้ง service + health monitor
.\deploy\windows\install-nssm-services.ps1 -NssmPath "C:\nssm\win64\nssm.exe"

# อนุญาต degraded mode ชั่วคราว (ถ้าต้องการ)
.\deploy\windows\install-nssm-services.ps1 -NssmPath "C:\nssm\win64\nssm.exe" -AllowAuthDegraded
```

## 7.4 ตรวจสอบหลังติดตั้ง

```powershell
# ดู service
Get-Service CryptoBotRuntime

# ดู scheduled task
Get-ScheduledTask -TaskName CryptoBotHealthMonitor

# ดู health
Invoke-RestMethod http://127.0.0.1:8080/health

# รัน preflight
.\.venv-3\Scripts\python.exe scripts\vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --json
```

## 7.5 Logs และ State Files

```
logs/services/runtime-service.log
logs/services/runtime-service.err.log
logs/windows-service-health-monitor.log
logs/windows-service-health-state.json
```

## 7.6 Restart Manual

```powershell
# วิธีที่แนะนำ
.\deploy\windows\restart-runtime-service.ps1

# หรือ restart service ตรงๆ
Restart-Service CryptoBotRuntime
```

## 7.7 ถอดติดตั้ง

```powershell
Set-Location "C:\path\to\Crypto_Sniper"
.\deploy\windows\uninstall-nssm-services.ps1 -NssmPath "C:\nssm\win64\nssm.exe"
```

---

# 8. การย้ายระบบไปเครื่องใหม่

## 8.1 ข้อห้ามสำคัญ

- ❌ ห้ามรัน bot 2 เครื่องพร้อมกันด้วย API key ชุดเดียว
- ❌ ห้าม copy database ตอน process กำลังเขียน

## 8.2 ขั้นตอนการย้าย

### บนเครื่องเดิม - หยุดระบบ

```powershell
# Standalone mode
Ctrl+C ในหน้าต่างที่รัน main.py

# หรือ Windows service
Stop-Service CryptoBotRuntime
```

### สิ่งที่ต้องย้าย

| ไฟล์ | ความสำคัญ |
|------|----------|
| โฟลเดอร์โปรเจกต์ทั้งหมด | ต้องย้าย |
| `.env` | ต้องย้าย |
| `bot_config.yaml` | ต้องย้าย |
| `coin_whitelist.json` | ต้องย้าย |
| `crypto_bot.db` | ต้องย้าย |
| `risk_state.json` | ต้องย้าย |
| `balance_monitor_state.json` | ต้องย้าย |

### ถ้าหยุดไม่ทัน (live snapshot)

ต้องย้ายทั้ง 3 ไฟล์พร้อมกัน:
- `crypto_bot.db`
- `crypto_bot.db-wal`
- `crypto_bot.db-shm`

### สิ่งที่ไม่ต้องย้าย

- `.venv/` หรือ `.venv-3/`
- `venv/`
- `__pycache__/`
- `.pytest_cache/`

ถ้าต้องการโฟลเดอร์เล็กลงก่อน copy:

```powershell
.\scripts\cleanup_before_transfer.ps1
```

### บนเครื่องใหม่ - ติดตั้ง

```powershell
# 1. วางโปรเจกต์
Set-Location "C:\Users\YourUser\Desktop\Crypto_Sniper"

# 2. สร้าง venv ใหม่
python -m venv .venv-3
.\activate_env.ps1
pip install -r requirements.txt

# 3. ตรวจ .env
# เพิ่ม IP เครื่องใหม่ใน Bitkub allowlist

# 4. ทดสอบเปิด
$env:BOT_STARTUP_TEST_MODE = "1"
.\.venv-3\Scripts\python.exe main.py

# 5. ตรวจ health
Invoke-RestMethod http://127.0.0.1:8080/health

# 6. รัน preflight
.\.venv-3\Scripts\python.exe scripts\vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --json

# 7. ถ้าใช้ Windows services
.\deploy\windows\install-nssm-services.ps1 -NssmPath "C:\nssm\win64\nssm.exe"
```

## 8.3 Checklist สั้นๆ

1. [ ] หยุด bot เครื่องเก่าให้หมด
2. [ ] copy โฟลเดอร์โปรเจกต์ + config files + database
3. [ ] สร้าง `.venv-3` ใหม่บนเครื่องใหม่
4. [ ] `pip install -r requirements.txt`
5. [ ] เพิ่ม IP เครื่องใหม่ใน Bitkub allowlist
6. [ ] ทดสอบด้วย `BOT_STARTUP_TEST_MODE=1`
7. [ ] รัน preflight ให้ผ่าน
8. [ ] เปิดใช้งานจริงเมื่อยืนยันเครื่องเก่าหยุดแล้ว

---

# 9. การดูแลรักษาและแก้ไขปัญหา

## 9.1 การดูแลระบบประจำวัน

### ทุกครั้งที่เปิด Bot

```powershell
# 1. ตรวจ health
Invoke-RestMethod http://127.0.0.1:8080/health

# 2. รัน preflight
.\.venv-3\Scripts\python.exe scripts\vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --json

# 3. ดู Rich terminal
# - Portfolio Breakdown
# - Open Positions
# - Pair list
```

### Logs ที่ควรดู

- `logs/services/` - Windows service logs
- `journalctl -u crypto-bot-tmux` - Linux service logs
- ดู errors ที่เกี่ยวกับ:
  - pair resolution
  - rebalance scope
  - BUY rejection / portfolio guard behavior

## 9.2 ปัญหาที่พบบ่อย

### ปัญหา: Bot ไม่เปิด

**สาเหตุ:** `.env` หรือ config ผิดพลาด

**วิธีแก้:**
```powershell
# ตรวจสอบ .env
cat .env

# ทดสอบ safe mode
$env:BOT_STARTUP_TEST_MODE = "1"
.\.venv-3\Scripts\python.exe main.py
```

### ปัญหา: Health เป็น degraded

**สาเหตุ:** Bitkub private auth ไม่พร้อม

**วิธีแก้:**
1. ตรวจ API key ใน `.env`
2. ตรวจ IP allowlist บน Bitkub
3. รอ private API พร้อม หรือใช้ `--allow-auth-degraded` ชั่วคราว

### ปัญหา: ขาดทุนเกินที่กำหนด

**สาเหตุ:** Risk config ไม่เหมาะสม

**วิธีแก้:**
```yaml
risk:
  max_daily_loss_pct: 5.0  # ลดลง
  max_risk_per_trade_pct: 2.0  # ลดลง
```

### ปัญหา: Bot เปิด position ผิดเหรียญ

**สาเหตุ:** held_coins_only ไม่ได้เปิด

**วิธีแก้:**
```yaml
data:
  portfolio_guard:
    held_coins_only: true
  hybrid_dynamic_coin_config:
    whitelist_json_path: "coin_whitelist.json"
```

## 9.3 การ Backup

```powershell
# Backup database
copy crypto_bot.db crypto_bot_backup_$(Get-Date -Format "yyyyMMdd").db

# Backup state files
copy risk_state.json risk_state_backup.json
copy balance_monitor_state.json balance_monitor_backup.json
```

## 9.4 การ Update

```powershell
# 1. หยุด bot
# Ctrl+C หรือ Stop-Service

# 2. Pull latest code
git pull

# 3. Update dependencies
pip install -r requirements.txt

# 4. ทดสอบ
$env:BOT_STARTUP_TEST_MODE = "1"
.\.venv-3\Scripts\python.exe main.py

# 5. ถ้าใช้ Windows service
.\deploy\windows\restart-runtime-service.ps1
```

---

# 10. รายงานการตรวจสอบล่าสุด

## 10.1 สิ่งที่ยืนยันเพิ่มแล้ว (14 เมษายน 2569)

### ✅ Final verification และ live runtime proof
- รัน full repo test suite ผ่าน `333 passed, 11 skipped, 2 warnings`
- รัน post-fix validation เพิ่มอีก `33 passed` สำหรับ CLI runtime และ `84 passed` สำหรับ integration slice
- ตรวจ VPS runtime จริงแล้ว health ยังเป็น `healthy: true`, `auth_degraded.active: false`, และ `tradable_pairs` ครบ
- ทำ live operational drill ด้วยเงินจริงจำนวนน้อยเพื่อพิสูจน์ buy -> track -> close flow จริง
- เจอ bug จริงใน manual CLI market BUY/close lifecycle แล้วแก้ root cause สำเร็จ
- redeploy ขึ้น VPS และยืนยันซ้ำว่าปิด position by order id ได้จริง, `orders` กลับ `none`, `open_positions` กลับ `0`

ดูรายละเอียดเต็มได้ที่ `docs/FINAL_VERIFICATION_20260414_TH.md`

## 10.2 สิ่งที่แก้ไขแล้วก่อนหน้า (13 เมษายน 2569)

### ✅ Rich CLI / Dashboard Fixes
- แก้ไข empty-state row ใน Signal Alignment table
- เพิ่ม render timing warnings สำหรับ `live.update()`
- เพิ่ม snapshot timing warnings
- เพิ่ม CLI listener auto-restart
- แก้ไข scalping profile `max_trades_per_day` เป็น 50

### ✅ WebSocket Fixes
- ปรับปรุง reconnect log ให้ตรงกับ logic จริง
- ป้องกัน unnecessary reconnect
- หยุด clear cache บน `stop()`
- เพิ่ม heartbeat-thread handoff protection

### ✅ Dead Code Cleanup
- ลบ redundant trailing comments
- Implement `PositionManager.sync_from_database()`
- Implement `PositionManager.reconcile_with_exchange()`
- เปลี่ยน `except Exception: pass` เป็น explicit logging

## 10.3 สถานะ Bot ปัจจุบัน

| หัวข้อ | สถานะ |
|--------|--------|
| Architecture | ✅ ดี |
| Error Handling | ✅ ดี |
| Reconciliation | ✅ แข็งแกร่ง |
| Dead Code | ✅ Clean แล้ว |
| Position Manager | ✅ มี implementation จริง |
| CLI Dashboard | ✅ ทำงานได้ |
| Final Verification | ✅ ผ่านทั้ง local และ live runtime |
| Manual CLI Execution Path | ✅ buy/close-by-id ผ่านหลัง fix |

## 10.4 สิ่งที่ควรทำต่อ

| ลำดับ | สิ่งที่ต้องทำ | ความสำคัญ |
|-------|---------------|-----------|
| 1 | Audit `except Exception: pass` ทั้งหมด | สูง |
| 2 | ตัดสินใจ `PositionManager` เป็น component จริงหรือไม่ | ปานกลาง |
| 3 | ลด shutdown latency ใน `BalanceMonitor` | ต่ำ |
| 4 | เพิ่ม tests สำหรับ `PositionManager` | ปานกลาง |

---

# 📞 ข้อมูลติดต่อและแหล่งข้อมูลเพิ่มเติม

## ไฟล์ที่เกี่ยวข้อง

| ไฟล์ | รายละเอียด |
|------|------------|
| `README.md` | ภาพรวมโปรเจกต์ |
| `bot_config.yaml` | คอนฟิกหลัก |
| `.env.example` | ตัวอย่าง environment variables |
| `coin_whitelist.json` | รายชื่อเหรียญที่อนุญาต |

## Scripts ที่มีประโยชน์

| Script | การใช้งาน |
|--------|----------|
| `scripts/vps_preflight.py` | ตรวจสอบความพร้อมก่อน live |
| `scripts/cleanup_before_transfer.ps1` | ทำความสะอาดก่อนย้ายเครื่อง |
| `deploy/windows/install-nssm-services.ps1` | ติดตั้ง Windows service |
| `deploy/windows/restart-runtime-service.ps1` | Restart bot |

## Health Endpoint

```
http://127.0.0.1:8080/health
```

**ค่าที่ต้องการ:**
- `healthy: true`
- `status: ok` (ไม่ใช่ `degraded`)

---

**จัดทำเมื่อ:** 14 เมษายน 2569  
**เวอร์ชัน Bot:** 1.0  
**สถานะ:** ✅ Production Ready