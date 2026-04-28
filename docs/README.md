# ดัชนีเอกสาร (Documentation index)

คู่มือและรายงานทั้งหมดของโปรเจกต์ **Binance Thailand / Crypto Bot V1** (terminal-first, headless runtime)  
อัปเดตล่าสุดเพื่อสะท้อนโครงสร้างโค้ดหลัง refactor: แพ็กเกจ `[trading/bot_runtime/](../trading/bot_runtime/)` แยก logic จาก `[trading_bot.py](../trading_bot.py)` ออกเป็นชิ้นย่อย (ดู [ADR-001](ADR-001-domain-boundaries-and-dependencies.md))

---

## เริ่มต้นและใช้งานประจำวัน


| เอกสาร                                             | รายละเอียด                                                |
| -------------------------------------------------- | --------------------------------------------------------- |
| [DAILY_QUICK_START_TH.md](DAILY_QUICK_START_TH.md) | คำสั่งสั้นๆ สำหรับรัน/เช็กบอทแบบรวดเร็ว                   |
| [MANUAL_THAI.md](MANUAL_THAI.md)                   | คู่มือภาษาไทยแบบเต็ม: โครงสร้างโปรเจกต์, flow, การตั้งค่า |
| [CONFIGURATION_SCHEMA.md](CONFIGURATION_SCHEMA.md) | รายการฟิลด์หลักใน `bot_config.yaml` และความหมาย           |


---

## สถาปัตยกรรมและขอบเขตโดเมน


| เอกสาร                                                                                         | รายละเอียด                                                                               |
| ---------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| [ADR-001-domain-boundaries-and-dependencies.md](ADR-001-domain-boundaries-and-dependencies.md) | แผนที่โดเมน, dependency, กฎ refactor, ตารางโมดูล `trading/bot_runtime/`*, แผนภาพ Mermaid |


---

## การรันบน Windows / VPS / Production


| เอกสาร                                                                 | รายละเอียด                                     |
| ---------------------------------------------------------------------- | ---------------------------------------------- |
| [WINDOWS_ALWAYS_ON_SETUP_TH.md](WINDOWS_ALWAYS_ON_SETUP_TH.md)         | ติดตั้งบริการ Windows (NSSM), รันแบบ always-on |
| [WINDOWS_TRANSFER_WITH_STATE_TH.md](WINDOWS_TRANSFER_WITH_STATE_TH.md) | ย้ายเครื่อง/โฟลเดอร์พร้อม state                |
| [VPS_PREFLIGHT_CHECKLIST.md](VPS_PREFLIGHT_CHECKLIST.md)               | Checklist ก่อนขึ้น VPS                         |
| [VPS_GO_LIVE_CHECKLIST_TH.md](VPS_GO_LIVE_CHECKLIST_TH.md)             | Checklist ก่อน go-live                         |
| [PRODUCTION_DEPLOYMENT_SUMMARY.md](PRODUCTION_DEPLOYMENT_SUMMARY.md)   | สรุปการ deploy production                      |


---

## Held-coins / Portfolio guard


| เอกสาร                                                   | รายละเอียด                              |
| -------------------------------------------------------- | --------------------------------------- |
| [HELD_COINS_ONLY_TRADING.md](HELD_COINS_ONLY_TRADING.md) | พฤติกรรมโหมดเทรดเฉพาะเหรียญที่เคยถือ    |
| [HELD_COINS_TECHNICAL.md](HELD_COINS_TECHNICAL.md)       | รายละเอียดเชิงเทคนิคและจุด guard ในโค้ด |
| [HELD_COINS_QUICK_REF.md](HELD_COINS_QUICK_REF.md)       | อ้างอิงสั้น                             |


---

## โหมดสลับ strategy / Bitkub legacy


| เอกสาร                                                                         | รายละเอียด                                                                |
| ------------------------------------------------------------------------------ | ------------------------------------------------------------------------- |
| [AUTO_MODE_SWITCHING_IMPLEMENTATION.md](AUTO_MODE_SWITCHING_IMPLEMENTATION.md) | การสลับโหมดกลยุทธ์อัตโนมัติ                                               |
| [BITKUB_CONFIG_VALIDATION.md](BITKUB_CONFIG_VALIDATION.md)                     | Validation คอนฟิก (legacy Bitkub naming; runtime ปัจจุบันเน้น Binance TH) |


---

## การตรวจสอบ live / รายงานรีวิว


| เอกสาร                                                                 | รายละเอียด                                                |
| ---------------------------------------------------------------------- | --------------------------------------------------------- |
| [LIVE_FLOW_VERIFICATION_TH.md](LIVE_FLOW_VERIFICATION_TH.md)           | ตรวจสอบ flow การเทรดจริง                                  |
| [FINAL_VERIFICATION_20260414_TH.md](FINAL_VERIFICATION_20260414_TH.md) | รายงาน verification วันที่ระบุ                            |
| [REVIEW_SUMMARY_TH.md](REVIEW_SUMMARY_TH.md)                           | สรุปรีวิว — ดูหมายเหตุด้านล่างเรื่องขนาด `trading_bot.py` |
| [DEEP_REVIEW_REPORT.md](DEEP_REVIEW_REPORT.md)                         | รีวิวเชิงลึก                                              |
| [DETAILED_FINDINGS_20260413.md](DETAILED_FINDINGS_20260413.md)         | รายการค้นพบรายละเอียด (snapshot วันที่)                   |
| [COMPLETION_REPORT.md](COMPLETION_REPORT.md)                           | รายงานความสมบูรณ์ของงาน                                   |
| [CODE_QUALITY_SCORE.md](CODE_QUALITY_SCORE.md)                         | คะแนนคุณภาพโค้ด                                           |
| [SECURITY_AUDIT_REPORT.md](SECURITY_AUDIT_REPORT.md)                   | รายงาน audit ความปลอดภัย                                  |


---

## โครงสร้างโค้ดที่เกี่ยวกับ Orchestrator (อ้างอิงสั้น)


| ตำแหน่ง                        | บทบาท                                                                                                                      |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| `main.py`                      | `TradingBotApp` — โหลด config, collector, CLI, สร้าง `TradingBotOrchestrator`                                              |
| `trading_bot.py`               | คลาส `TradingBotOrchestrator` — facade บางส่วน delegate ไปที่ `trading/bot_runtime/*`                                      |
| `trading/bot_runtime/`         | ชิ้นย่อย: main loop, WebSocket, iteration, pause state, pre-trade gate, order logging, pairs/candle filter, exit gates ฯลฯ |
| `trading/signal_runtime.py`    | ประมวลผลรายคู่, execution plan, portfolio guard บน BUY                                                                     |
| `trading/execution_runtime.py` | full/semi/dry run และ pending decisions                                                                                    |
| `trade_executor.py`            | OMS                                                                                                                        |


รายละเอียดแยกไฟล์ใน `trading/bot_runtime/` อยู่ในตารางภายใน [ADR-001](ADR-001-domain-boundaries-and-dependencies.md)

---

## หมายเหตุการบำรุงรักษาเอกสาร

- **หมายเลขบรรทัด** ในโค้ดอาจเปลี่ยนหลัง refactor — ใช้ชื่อไฟล์และสัญลักษณ์ฟังก์ชันเป็นหลัก
- เอกสารที่มีวันที่ในชื่อไฟล์คือ **snapshot** ของช่วงเวลานั้น ไม่จำเป็นต้องตรงกับโค้ดล่าสุดทุกบรรทัด
- เอกสารหลักที่ควร sync กับโครงสร้างปัจจุบัน: **README ราก**, **MANUAL_THAI**, **ADR-001**, **ดัชนีนี้** (`docs/README.md`)