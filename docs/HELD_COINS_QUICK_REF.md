# Held Coins Quick Reference

## Goal

ให้ runtime เทรดหรือจัดการเฉพาะเหรียญที่สอดคล้องกับ holdings และ whitelist ที่คุณตั้งใจไว้

## Primary Files

- `bot_config.yaml`
- `coin_whitelist.json`
- `main.py`
- `trading_bot.py`

## What To Check

### Current Whitelist Profile

runtime profile ที่ใช้อยู่ตอนนี้ใน `coin_whitelist.json`:

- `BTC`
- `ETH`
- `BNB`
- `SOL`
- `XRP`
- `ADA`
- `DOGE`
- `LINK`
- `MATIC`

### Config

```yaml
data:
  auto_detect_held_pairs: true
  pairs: []
  hybrid_dynamic_coin_config:
    whitelist_json_path: "coin_whitelist.json"
    include_assets_with_balance: true
```

### Runtime

- Rich terminal `Portfolio Breakdown`
- runtime logs
- bot health endpoint

## Quick Validation

```powershell
$env:BOT_STARTUP_TEST_MODE = "1"
.\.venv-3\Scripts\python.exe main.py
```

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health
```

```powershell
.\.venv-3\Scripts\python.exe scripts/vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --json
```

## Good Signs

- Runtime starts without config/auth surprises
- Health endpoint is reachable
- Pair resolution matches your holdings/whitelist intent
- Portfolio panel reflects the assets you expect

## Bad Signs

- Runtime includes coins outside your intended scope
- `status=degraded` when you expected live-ready auth
- BUY behavior appears on assets you do not intend to manage
