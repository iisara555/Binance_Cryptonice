# Crypto Trading Bot Configuration Schema

Full documented configuration reference for the current standalone runtime.

---

## Table of Contents
1.  [Global Bot Configuration](#global-bot-configuration)
2.  [Trading Configuration](#trading-configuration)
3.  [Risk Management](#risk-management)
4.  [Strategies Configuration](#strategies-configuration)
5.  [Signal Source Configuration](#signal-source-configuration)
6.  [Logging & Metrics](#logging--metrics)
7.  [Notifications](#notifications)
8.  [Monitoring](#monitoring)
9.  [Portfolio Rebalancing](#portfolio-rebalancing)
10. [Backtesting Validation](#backtesting-validation)
11. [Full Example Configuration](#full-example-configuration)

---

## Global Bot Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `mode` | string | `semi_auto` | Bot operation mode: `full_auto`, `semi_auto`, `dry_run` |
| `trading_pair` | string | `""` | Primary trading pair symbol; runtime may fill this from holdings |
| `interval_seconds` | integer | `60` | Main loop execution interval in seconds |
| `timeframe` | string | `15m` | Candle timeframe |
| `read_only` | boolean | `false` | Read-only mode (no trades executed) |

---

## Trading Configuration

```yaml
trading:
  trading_pair: ""
  interval_seconds: 60
  timeframe: "15m"
  mode: "full_auto"
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `trading_pair` | string | Runtime pair override |
| `interval_seconds` | integer | Main loop interval |
| `timeframe` | string | Primary timeframe |
| `mode` | string | `dry_run`, `semi_auto`, `full_auto` |

---

## Risk Management

```yaml
risk:
  max_risk_per_trade_pct: 4.0
  max_daily_loss_pct: 10.0
  max_position_per_trade_pct: 10.0
  max_open_positions: 4
```

สำคัญที่สุดสำหรับ live safety คือ daily loss limit, max position per trade, max open positions, และ cooldown

---

## Strategies Configuration

```yaml
strategies:
  enabled:
    - trend_following
    - mean_reversion
    - breakout
    - scalping
  min_confidence: 0.35
  min_strategies_agree: 2
```

---

## Signal Source Configuration

repo ปัจจุบันใช้ strategy engine เป็น signal source หลัก ไม่มี AI/ML runtime หรือ `ai_signals/` แล้ว

```yaml
multi_timeframe:
  enabled: true
  require_htf_confirmation: true
```

ให้ปรับ threshold และ logic ผ่าน `strategies.*` และ `multi_timeframe.*` ใน `bot_config.yaml`

---

## Logging & Metrics

```yaml
logging:
  log_level: "INFO"
  enable_console: true
  max_log_size_mb: 100
  backup_count: 10
```

See `logger_setup.py` for the runtime logging configuration.

---

## Notifications

```yaml
notifications:
  alert_channel: "telegram"
  telegram_command_polling_enabled: false
  send_alerts: true
```

`telegram_command_polling_enabled: false` keeps outbound Telegram alerts available but disables long-poll command handling.

---

## Monitoring

```yaml
monitoring:
  enabled: true
  health_check_port: 8080
  health_check_path: "/health"
```

นี่คือ health endpoint ที่ preflight และ service monitors ใช้ตรวจ runtime

---

## Portfolio Rebalancing

```yaml
rebalance:
  enabled: true
  strategy: "threshold"
  check_interval: 5
  target_allocation:
    THB: 20.0
    BTC: 40.0
    ETH: 40.0
```

ปรับ `threshold`, `calendar`, `risk`, และ `target_allocation` ตามพอร์ตจริงของคุณ

---

## Backtesting Validation

```yaml
backtesting:
  require_validation_before_live: false
```

เก็บไว้สำหรับ gating policy ก่อน live mode แม้ runtime ปัจจุบันจะเป็น strategy-first

---

## Full Example Configuration

```yaml
trading:
  trading_pair: ""
  interval_seconds: 60
  timeframe: "15m"
  mode: "dry_run"

strategies:
  enabled:
    - trend_following
    - mean_reversion
    - breakout
    - scalping
  min_confidence: 0.35
  min_strategies_agree: 2

multi_timeframe:
  enabled: true

risk:
  max_risk_per_trade_pct: 4.0
  max_daily_loss_pct: 10.0
  max_position_per_trade_pct: 10.0
  max_open_positions: 4

notifications:
  alert_channel: "telegram"
  telegram_command_polling_enabled: false
  send_alerts: true

monitoring:
  enabled: true
  health_check_port: 8080
  health_check_path: "/health"

rebalance:
  enabled: true
  strategy: "threshold"
  target_allocation:
    THB: 20.0
    BTC: 40.0
    ETH: 40.0

backtesting:
  require_validation_before_live: false
```

---

## Validation

Configuration is validated by the runtime on startup. For operational checks, use:

```powershell
.\.venv-3\Scripts\python.exe scripts/vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --json
```
