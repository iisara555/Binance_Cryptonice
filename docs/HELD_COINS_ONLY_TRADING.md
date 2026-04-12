# Held Coins Only Trading

เอกสารนี้อธิบายโหมดการใช้งานที่ต้องการให้บอทจัดการเฉพาะเหรียญที่ถืออยู่จริง และไม่เปิดสถานะใหม่ในเหรียญที่อยู่นอกขอบเขต holdings / whitelist ที่ตั้งใจไว้

## Why Use This Mode

- ลดโอกาสเปิด position ใหม่ในเหรียญที่คุณไม่ได้ตั้งใจถือ
- ทำให้ rebalance และ managed exits อยู่ในกรอบพอร์ตปัจจุบัน
- เหมาะกับการย้ายจาก manual portfolio มาเป็น bot-assisted management

## Runtime Signals That Matter

ใน repo ปัจจุบันไม่ต้องพึ่ง `validate_bitkub_config.py` แล้ว ให้ตรวจจาก runtime จริงแทน

### 1. Check runtime health

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health
```

### 2. Check terminal portfolio panel

Rich terminal จะแสดง:

- `Total Balance`
- `Portfolio Breakdown`
- allocation ต่อเหรียญ

### 3. Watch runtime logs

ให้ดู log ที่เกี่ยวกับ:

- pair resolution
- whitelist hot reload
- rejected BUY signals
- rebalance filtering

## Expected Behavior

เมื่อระบบถูกตั้งให้ทำงานกับ held coins only:

- เหรียญที่ไม่มี position/holding จริงไม่ควรถูกเปิด BUY ใหม่ง่าย ๆ
- rebalance จะ focus ที่สินทรัพย์ในกรอบพอร์ตปัจจุบัน
- pair list runtime ควรถูกจำกัดด้วย holdings และ whitelist config

## Config Areas To Review

```yaml
data:
  auto_detect_held_pairs: true
  pairs: []
  portfolio_guard:
    held_coins_only: true
  hybrid_dynamic_coin_config:
    whitelist_json_path: "coin_whitelist.json"
    include_assets_with_balance: true
    min_quote_balance_thb: 100.0

rebalance:
  enabled: true
  target_allocation:
    THB: 20.0
    BTC: 80.0
```

## Verification Workflow

1. Confirm current holdings via exchange / runtime state.
2. Review `coin_whitelist.json`.
3. Start in safe mode.
4. Check Rich terminal `Portfolio Breakdown`.
5. Review logs to ensure the runtime pair set matches intent.

## Safe Startup

```powershell
$env:BOT_STARTUP_TEST_MODE = "1"
.\.venv-3\Scripts\python.exe main.py
```

## Important Note

ถ้าคุณต้องการ “ไม่เปิดเหรียญใหม่เลย” จริง ๆ ต้อง review ทั้ง config และ runtime behavior ร่วมกัน ไม่ใช่ดูเพียง static config file อย่างเดียว

ถ้าคุณต้องการให้บอทเปิด BUY เหรียญใหม่จาก whitelist ได้ ให้ตั้ง `data.portfolio_guard.held_coins_only: false` ใน `bot_config.yaml`
