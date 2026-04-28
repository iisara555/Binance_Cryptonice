# Held Coins Only Mode - Technical Notes

เอกสารฉบับนี้แทน technical write-up รุ่นเก่าที่อ้าง line numbers และโค้ด snapshot ในอดีต ซึ่งไม่เหมาะกับ repo ที่ refactor ต่อเนื่อง

## Objective

ให้ runtime จำกัดการจัดการพอร์ตอยู่ในกรอบ holdings / whitelist ที่ตั้งใจไว้ และลดโอกาสเปิดสถานะใหม่ในเหรียญที่อยู่นอกขอบเขตนั้น

## Current Technical Shape

แนวคิดนี้กระจายอยู่ใน 3 จุดหลักของระบบ:

1. pair resolution runtime
2. rebalance allocation filtering
3. execution-plan guards สำหรับ BUY flows

## 1. Runtime Pair Resolution

ไฟล์ที่เกี่ยวข้อง:

- `main.py`
- `dynamic_coin_config.py`
- `coin_whitelist.json`

บทบาท:

- resolve pair set จาก holdings จริงและ whitelist
- keep held assets in scope แม้ quote balance จะต่ำ
- hot reload pair list ได้โดยไม่ restart เมื่อ config JSON เปลี่ยน

current whitelist profile:

- `BTC`
- `ETH`
- `BNB`
- `SOL`
- `XRP`
- `ADA`
- `DOGE`
- `LINK`
- `POL`

จุด config ที่เกี่ยวข้อง:

```yaml
data:
  auto_detect_held_pairs: true
  pairs: []
  hybrid_dynamic_coin_config:
    whitelist_json_path: "coin_whitelist.json"
    include_assets_with_balance: true
    min_quote_balance_thb: 100.0
```

## 2. Rebalance Allocation Filtering

ไฟล์ที่เกี่ยวข้อง:

- `portfolio_rebalancer.py`
- `portfolio_manager.py`

บทบาท:

- จำกัดการคำนวณ allocation ให้อยู่กับสินทรัพย์ที่ runtime มองว่าอยู่ในพอร์ตจริง
- ข้ามสินทรัพย์ที่ไม่มี quantity จริงหรือข้อมูลไม่พร้อม
- log scope ของ held coins และ skipped coins ชัดเจนขึ้น

สิ่งที่ควรเห็นใน log:

- held coins ที่ถูกนำมาพิจารณา rebalancing
- coins ที่ถูกข้ามเพราะไม่ได้ถืออยู่
- coins ที่ถูกข้ามเพราะข้อมูลไม่พอ

## 3. Execution Plan Guard

ไฟล์ที่เกี่ยวข้อง:

- `trading_bot.py` (`TradingBotOrchestrator`) และแพ็กเกจ [`trading/bot_runtime/`](../trading/bot_runtime/) (เช่น `run_iteration_runtime` — กรองคู่จาก `held_coins_only`)
- `trading/signal_runtime.py` — guard BUY เมื่อ `held_coins_only` และประวัติการถือเหรียญ
- `trade_executor.py`

บทบาท:

- ป้องกัน BUY execution path บางกรณีไม่ให้เปิดสถานะใหม่ในสินทรัพย์ที่อยู่นอกกรอบ held-coins intent
- คง SELL / managed exit path สำหรับตำแหน่งที่มีอยู่จริง

หลักคิดสำคัญ:

- BUY ใหม่ควรผ่าน runtime intent checks ก่อน
- managed exit ไม่ควรถูก block ถ้ามี position จริง

## Runtime Verification

### Safe startup

```powershell
$env:BOT_STARTUP_TEST_MODE = "1"
.\.venv-3\Scripts\python.exe main.py
```

### Check health

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health
```

### Check terminal visibility

ใน Rich terminal ให้ดู:

- `Portfolio Breakdown`
- `Open Positions`
- pair list และ runtime status

### Check logs

ให้ review log ที่เกี่ยวข้องกับ:

- pair resolution
- rebalance scope
- BUY rejection / portfolio guard behavior

## Expected Operational Behavior

| Scenario | Expected behavior |
|----------|-------------------|
| Held asset receives managed exit | Allowed |
| Held asset is part of rebalance scope | Allowed |
| Non-held asset appears outside intended scope | Should be filtered or rejected by runtime intent |
| Whitelist changes at runtime | Pair scope refreshes without restart when hot reload is enabled |

## Safety Notes

- held-coins intent ไม่ได้มาจากจุดเดียว แต่เกิดจากผลรวมของ config, runtime pair resolution, และ execution guards
- ถ้าจะใช้งานแบบ conservative จริง ควร review ทั้ง `coin_whitelist.json`, `bot_config.yaml`, Rich terminal และ logs พร้อมกัน
- อย่าใช้เอกสาร snapshot รุ่นเก่าที่อ้างเลขบรรทัดโค้ดเป็นแหล่ง truth เดียว เพราะมีการ refactor — อ้างจากชื่อไฟล์และฟังก์ชัน ดูภาพรวมใน [docs/README.md](README.md) และ [ADR-001](ADR-001-domain-boundaries-and-dependencies.md)
✓ **Rebalancing still works**  

---

## Summary of Changes

| เขตโค้ด | ประเภท | หมายเหตุ |
|---------|--------|-----------|
| `trading/signal_runtime.py` | BUY + `held_coins_only` | Execution-plan path — ปฏิเสธ BUY ในคู่ที่ไม่เคย held (ดู log `[Portfolio Guard]`) |
| `trading/bot_runtime/run_iteration_runtime.py` | กรองรายการคู่ก่อนวน `process_pair_iteration` | ข้ามคู่ที่ไม่เคย held เมื่อ guard เปิด |
| `trading/startup_runtime.py` | bootstrap ประวัติ held | Backfill / log ที่เกี่ยวกับ held-coin history |
| `portfolio_rebalancer.py` | Rebalancer scope | ปรับสมดุลตาม intent พอร์ต |

หมายเลขบรรทัดเก่าที่ระบุไว้ใน snapshot เดิมของเอกสารนี้อาจเปลี่ยนหลัง refactor — อ้างอิงตามชื่อไฟล์และฟังก์ชันข้างต้น

---

**Implementation**: Complete ✅  
**Testing**: Passed ✅  
**Ready**: Deploy now ✅

