# Production Deployment Summary

## Current Status

โปรเจกต์นี้อยู่ในสถานะ runtime-only สำหรับบอทเทรด Bitkub แบบ standalone โดย entrypoint หลักคือ `main.py` และ launcher บน Windows คือ `run_bot.bat` หรือ `deploy/windows/run-runtime.ps1`

เอกสารฉบับนี้แทนที่ summary รุ่นเก่าที่อ้าง dashboard, frontend, `start.py` และ AI/ML runtime ซึ่งไม่มีอยู่ใน repo ปัจจุบันแล้ว

ณ วันที่ 14 เมษายน 2569 ระบบนี้ผ่าน final verification รอบล่าสุดแล้ว ทั้งใน local suite และ live VPS runtime โดยดูรายละเอียดเต็มได้ที่ [FINAL_VERIFICATION_20260414_TH.md](FINAL_VERIFICATION_20260414_TH.md)

## Production Scope ที่ยังมีอยู่จริง

- Strategy-based signal generation
- Multi-timeframe analysis
- Risk management และ state persistence
- Trade execution และ position tracking
- Portfolio rebalancing
- Balance monitor
- Telegram alerts และ command polling แบบเลือกเปิดได้
- Rich terminal command center
- Bot health endpoint
- Windows NSSM runtime service
- Linux systemd runtime service

## Runtime Entry Points

### Local / Standalone

```powershell
.\run_bot.bat
```

หรือ

```powershell
.\.venv\Scripts\python.exe main.py
```

### Windows Always-On

```powershell
.\deploy\windows\install-nssm-services.ps1 -NssmPath "C:\nssm\win64\nssm.exe"
```

### Linux / VPS

ใช้ [deploy/systemd/crypto-bot-tmux.sh](../deploy/systemd/crypto-bot-tmux.sh) คู่กับ [deploy/systemd/crypto-bot-tmux.service](../deploy/systemd/crypto-bot-tmux.service)

แนวทางนี้ให้ `systemd` auto-start ตอน boot และให้ `tmux` ถือ interactive terminal ของ bot ไว้เพื่อให้ Rich CLI attach กลับไปดูได้

## Readiness Checks

### Safe startup

```powershell
$env:BOT_STARTUP_TEST_MODE = "1"
.\.venv\Scripts\python.exe main.py
```

### Strict preflight

```powershell
.\.venv\Scripts\python.exe scripts/vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --json
```

### Allow degraded mode temporarily

```powershell
.\.venv\Scripts\python.exe scripts/vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --allow-auth-degraded --json
```

## Go-Live Conditions

- `.env` contains real Bitkub credentials
- Bitkub IP allowlist is correct
- `bot_config.yaml` is reviewed intentionally
- bot health returns `healthy: true`
- bot health does not report `status: degraded` for real live deployment
- `LIVE_TRADING` is enabled intentionally, not by accident

## Latest Verified Evidence

- full repo tests ล่าสุดผ่าน `372 passed, 11 skipped, 2 warnings`
- post-fix CLI lifecycle regression tests ผ่าน `33 passed`
- broader integration หลัง fix ผ่าน `84 passed`
- live VPS health ล่าสุดอยู่ที่ `healthy: true`, `auth_degraded.active: false`, `tradable_pairs` ครบ 9 คู่
- live close-by-id path ถูกพิสูจน์จริงหลัง redeploy และจบด้วย `Active orders: none` และ `open_positions: 0`

## Important Operational Notes

- Runtime path resolution is now project-root based, so folder rename or drive move is supported for standalone usage
- Runtime launchers and systemd templates now prefer `.venv` and fallback to `.venv-3` or `venv` when needed
- Windows service installs still need reinstall after moving the project, because Windows stores absolute paths in service registration
- Linux / VPS ที่ต้องการ Rich CLI ควรใช้ `tmux` session (`crypto`) และให้ `crypto-bot-tmux.service` เป็นคนสร้าง session ตอน boot แทนการรัน bot ตรงใต้ `systemd`
- Historical references to dashboard, frontend, `start.py`, `ai_signals/`, `show_bitkub_coins.py`, and `validate_bitkub_config.py` are obsolete for this repo version

## Recommended Docs

- [README.md](../README.md)
- [FINAL_VERIFICATION_20260414_TH.md](FINAL_VERIFICATION_20260414_TH.md)
- [DAILY_QUICK_START_TH.md](DAILY_QUICK_START_TH.md)
- [WINDOWS_ALWAYS_ON_SETUP_TH.md](WINDOWS_ALWAYS_ON_SETUP_TH.md)
- [VPS_PREFLIGHT_CHECKLIST.md](VPS_PREFLIGHT_CHECKLIST.md)
- [VPS_GO_LIVE_CHECKLIST_TH.md](VPS_GO_LIVE_CHECKLIST_TH.md)
