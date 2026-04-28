# VPS Go-Live Checklist

เช็กลิสต์สั้นสำหรับใช้หน้างานก่อนเปิดระบบจริงบน VPS

ดัชนีเอกสาร: [README.md](./README.md)

## ก่อน start service

- ยืนยันว่า Binance Thailand API key/secret ถูกต้องและ permission ตรงกับการใช้งานจริง
- ยืนยันว่า `.env` ถูกสร้างจาก `.env.example` และใส่ key จริงครบแล้ว
- ยืนยันว่า `LIVE_TRADING` เป็นค่าที่คุณตั้งใจจริง
- ยืนยันว่า `bot_config.yaml` ใช้ค่าที่ต้องการจริง โดยเฉพาะ `trading.mode`, `rebalance`, `monitoring`
- ถ้าต้องการ Rich CLI ให้ใช้ `crypto-bot-tmux.service` และ `tmux` session แทนการรัน bot ตรงใต้ `systemd`

## หลัง start service

- `systemctl status crypto-bot-tmux` ต้องขึ้น `active (exited)`
- `tmux list-sessions` ต้องเห็น session เช่น `crypto`
- `tmux attach -t crypto` ต้องเข้า Rich CLI ได้
- `curl http://127.0.0.1:8080/health` ต้องได้ `healthy: true`
- bot health ต้องไม่ขึ้น `status: degraded`

## ก่อนเปิด live จริง

- รัน strict preflight ให้ผ่าน

```bash
python scripts/vps_preflight.py \
  --bot-health-url http://127.0.0.1:8080/health \
  --json
```

- ตรวจว่า Telegram behavior เป็นไปตามที่ต้องการ จะเปิดหรือปิดก็ได้แต่ต้องตั้งใจ
- ตรวจว่าไม่มี stale auth error, database error หรือ startup error ใน journal/log ล่าสุด

## คำสั่งที่ใช้บ่อยหน้างาน

```bash
sudo systemctl restart crypto-bot-tmux
sudo journalctl -u crypto-bot-tmux -n 100 --no-pager
tmux list-sessions
tmux attach -t crypto
curl http://127.0.0.1:8080/health
```

## ถ้ายังไม่พร้อม live

- อย่าเปิด `LIVE_TRADING=true`
- ใช้ `BOT_READ_ONLY=true` หรือเริ่มด้วยโหมดทดสอบก่อน
- ถ้า bot health เป็น `degraded` ให้แก้ credentials, permissions หรือ private API ก่อน
