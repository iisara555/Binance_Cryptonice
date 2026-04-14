# Live Flow Verification Checklist

เช็กลิสต์นี้ใช้ยืนยัน flow ฝั่ง live runtime แบบเป็นขั้นตอน หลัง deploy แล้วก่อนปล่อยให้ระบบทำงานยาว

## 1. Runtime พร้อมก่อนส่ง order

- `curl http://127.0.0.1:8080/health` ต้องได้ `healthy: true`
- `auth_degraded.active` ต้องเป็น `false`
- `collector.running` ต้องเป็น `true`
- `bot.status.tradable_pairs` ต้องไม่ว่าง
- ใน tmux dashboard ต้องเห็น `API Latency` มีค่า และ `Candle Readiness` มากกว่า `0/x ready`

## 2. Verify signal-to-decision flow

- ดู `Signal Alignment` ใน tmux ว่ามีอย่างน้อย 1 คู่ที่ `TF` พร้อมครบและสถานะ `Ready`
- ตรวจ log ล่าสุดว่ามี loop ปกติ ไม่มี `insufficient_data` ทุกคู่ที่ต้องการเทรด
- ถ้าคู่ยังอยู่ใน tracked pairs แต่ไม่อยู่ใน `tradable_pairs` ให้ถือว่าโดน Candle Guard กันไว้ ไม่ใช่ execution bug

คำสั่ง

```bash
curl -fsS http://127.0.0.1:8080/health | python3 -m json.tool
tmux capture-pane -pt crypto:0 -S -220
sudo journalctl -u crypto-bot-tmux -n 200 --no-pager
```

## 3. Verify order placement flow

- เมื่อมี BUY/SELL decision จริง ต้องเห็น log แนว `[FULL_AUTO]` หรือ execution log ที่ระบุ side, symbol, entry price
- หลังส่ง order ต้องมี order id ใน executor/OMS path
- ต้องไม่มี duplicate execution สำหรับ order เดียวกัน

สิ่งที่ต้องดู

- log ฝั่ง `trading_bot`
- log ฝั่ง `trade_executor`
- ตาราง orders/trades ใน SQLite

คำสั่ง

```bash
sudo journalctl -u crypto-bot-tmux -n 300 --no-pager | grep -E "FULL_AUTO|Trade Decision|OMS|filled|cancel|timeout"
python3 - <<'PY'
import sqlite3
conn = sqlite3.connect('/root/Crypto_Sniper/crypto_bot.db')
cur = conn.cursor()
for sql in [
    "SELECT id, symbol, side, status, created_at FROM orders ORDER BY created_at DESC LIMIT 10",
    "SELECT symbol, side, status, created_at FROM trades ORDER BY created_at DESC LIMIT 10",
]:
    print('\nSQL:', sql)
    for row in cur.execute(sql):
        print(row)
conn.close()
PY
```

## 4. Verify fill-to-position flow

- partial fill ต้องไม่ทำให้ remaining amount ติดลบ
- filled order ต้องไปอยู่ใน tracked/open position state
- `Open Positions` ใน dashboard ต้องมี `Entry`, `Current`, `SL/TP` ครบ

## 5. Verify exit and reconcile flow

- TP/SL hit แล้วต้องเห็น close path ใน executor/state machine
- order ที่ถูก fill/cancel แล้วต้องไม่ค้างเป็น stale open order
- restart service แล้ว reconcile ต้องไม่สร้าง ghost position ซ้ำ

คำสั่ง

```bash
sudo systemctl restart crypto-bot-tmux
curl -fsS http://127.0.0.1:8080/health | python3 -m json.tool
tmux capture-pane -pt crypto:0 -S -220
```

## 6. เกณฑ์ผ่านขั้นต่ำ

- health ปกติ
- มี `tradable_pairs` อย่างน้อย 1 คู่
- dashboard ไม่ขาด `API Latency`, `Current price`, `Total Balance`
- order lifecycle ใน log/DB ต่อกันครบ: decision -> placed -> filled/cancelled -> reconciled
- ไม่มี error ซ้ำเดิมเรื่อง timeout signature mismatch, bootstrap stale order, หรือ pair no-data ถูกเทรด