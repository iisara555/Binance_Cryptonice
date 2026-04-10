# Windows Always-On Setup

คู่มือนี้ใช้สำหรับรัน Crypto Bot V1 แบบ always-on บน Windows โดยให้:

- Trading runtime รันเป็น Windows service
- Scheduled Task คอยเช็ก bot health endpoint แล้ว restart runtime เมื่อระบบเสียต่อเนื่อง

โครงนี้เหมาะกับเครื่อง Windows ที่ต้องการให้ runtime กลับมาหลัง reboot หรือหลัง process crash โดยไม่ต้องเปิดหลาย terminal เอง

## สิ่งที่ใช้

- Windows PowerShell 5.1+
- Python venv ของโปรเจ็กต์ เช่น `.venv-3`
- NSSM (`nssm.exe`)
- สิทธิ Administrator ตอนติดตั้ง services และ scheduled task

สคริปต์ที่เกี่ยวข้อง:

- [deploy/windows/windows-service-config.psd1](../deploy/windows/windows-service-config.psd1)
- [deploy/windows/run-runtime.ps1](../deploy/windows/run-runtime.ps1)
- [deploy/windows/install-nssm-services.ps1](../deploy/windows/install-nssm-services.ps1)
- [deploy/windows/invoke-health-check.ps1](../deploy/windows/invoke-health-check.ps1)
- [deploy/windows/restart-runtime-service.ps1](../deploy/windows/restart-runtime-service.ps1)
- [deploy/windows/uninstall-nssm-services.ps1](../deploy/windows/uninstall-nssm-services.ps1)

## แนวคิดการทำงาน

1. Runtime service ใช้ root `main.py`
2. Health monitor เช็ก `http://127.0.0.1:8080/health`
3. ถ้ารันล้มเหลวต่อเนื่องครบ threshold จะ restart runtime service
4. ถ้าต้อง rollout โค้ดใหม่หรือบังคับ reload module แบบ clean ให้ใช้ wrapper restart แทนการ stop/start service เองกระจัดกระจาย

หมายเหตุ:

- Runtime health ค่า `status=degraded` จะถือว่าไม่ผ่านโดย default เว้นแต่จะติดตั้งด้วย `-AllowAuthDegraded`
- Windows service mode ยังต้อง reinstall service หากย้าย project root ไป path ใหม่ เพราะ Windows เก็บ absolute path ใน registry

## 1. ตั้งชื่อ service/task จากไฟล์เดียว

ถ้าต้องการเปลี่ยนชื่อ service หรือ scheduled task ให้แก้ไฟล์นี้ก่อน:

- [deploy/windows/windows-service-config.psd1](../deploy/windows/windows-service-config.psd1)

ค่าที่แก้บ่อย:

- `RuntimeServiceName`
- `HealthTaskName`
- `BotHealthUrl`

## 2. ติดตั้ง NSSM

ถ้ายังไม่มี NSSM ให้ติดตั้งก่อน เช่น:

- แตกไฟล์ไปที่ `C:\nssm\win64\nssm.exe`
- หรือใช้ winget: `winget install --id NSSM.NSSM -e`

จากนั้นเปิด PowerShell แบบ Run as Administrator

## 3. ติดตั้ง runtime service + health monitor

```powershell
Set-Location "C:\path\to\crypto-bot V1"

.\deploy\windows\install-nssm-services.ps1 \
  -NssmPath "C:\nssm\win64\nssm.exe"
```

ถ้าต้องการอนุญาต degraded mode ชั่วคราว:

```powershell
.\deploy\windows\install-nssm-services.ps1 \
  -NssmPath "C:\nssm\win64\nssm.exe" \
  -AllowAuthDegraded
```

ถ้าต้องการติดตั้ง service อย่างเดียวก่อน ยังไม่ start และยังไม่ register health task:

```powershell
.\deploy\windows\install-nssm-services.ps1 \
  -NssmPath "C:\nssm\win64\nssm.exe" \
  -SkipServiceStart \
  -SkipTaskRegistration
```

## 4. ตรวจว่า service ทำงานแล้ว

```powershell
Get-Service CryptoBotRuntime
Get-ScheduledTask -TaskName CryptoBotHealthMonitor
Invoke-RestMethod http://127.0.0.1:8080/health
```

ถ้าเปลี่ยนชื่อไว้ใน config file ให้ใช้ชื่อใหม่ใน `Get-Service` และ `Get-ScheduledTask`

ถ้าจะเช็กแบบรวมตาม workflow ของ repo:

```powershell
.\.venv-3\Scripts\python.exe scripts\vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --json
```

## 5. Logs และ state files

สคริปต์ชุดนี้จะเขียนไฟล์ที่:

- `logs/services/runtime-service.log`
- `logs/services/runtime-service.err.log`
- `logs/windows-service-health-monitor.log`
- `logs/windows-service-health-state.json`

## 6. Restart แบบ manual

```powershell
.\deploy\windows\restart-runtime-service.ps1
```

มี `restart-service-pair.ps1` คงไว้เป็น compatibility wrapper สำหรับ workflow เดิม แต่ชื่อหลักที่ควรใช้คือ `restart-runtime-service.ps1`

ยังสามารถเรียก low-level script ตรง ๆ ได้เช่นกัน:

```powershell
.\deploy\windows\invoke-health-check.ps1 -ForceRestart
```

หรือ restart service ตรง ๆ:

```powershell
Restart-Service CryptoBotRuntime
```

## 7. ถอดออก

```powershell
Set-Location "C:\path\to\crypto-bot V1"

.\deploy\windows\uninstall-nssm-services.ps1 \
  -NssmPath "C:\nssm\win64\nssm.exe"
```

## หมายเหตุสำคัญ

- อย่าพึ่ง shell เดิมที่จำ path เก่าไว้หลังย้ายโฟลเดอร์ ให้เปิด session ใหม่แล้วใช้ `.\activate_env.ps1` หรือ `.\run_bot.bat`
- อย่าถือว่า runtime `status=degraded` คือพร้อม live trading ถ้ายังไม่ได้ตั้งใจเปิด `-AllowAuthDegraded`
- ถ้าต้อง rollout โค้ดใหม่ ใช้ `restart-runtime-service.ps1` จะปลอดภัยกว่า restart แบบ ad-hoc
