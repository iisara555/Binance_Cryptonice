# Crypto Trading Bot Configuration Schema

Full documented configuration reference for the current standalone runtime.

---

## Table of Contents

1. [Global Bot Configuration](#global-bot-configuration)
2. [Trading Configuration](#trading-configuration)
3. [Risk Management](#risk-management)
4. [Strategies Configuration](#strategies-configuration)
5. [Signal Source Configuration](#signal-source-configuration)
6. [Logging & Metrics](#logging--metrics)
7. [Notifications](#notifications)
8. [Monitoring](#monitoring)
9. [Portfolio Rebalancing](#portfolio-rebalancing)
10. [Backtesting Validation](#backtesting-validation)
11. [Full Example Configuration](#full-example-configuration)
12. [Binance Thailand alignment (`bot_config.yaml`)](#binance-thailand-alignment-bot_configyaml)

---

## Global Bot Configuration


| Parameter          | Type    | Default     | Description                                                      |
| ------------------ | ------- | ----------- | ---------------------------------------------------------------- |
| `mode`             | string  | `semi_auto` | Bot operation mode: `full_auto`, `semi_auto`, `dry_run`          |
| `trading_pair`     | string  | `""`        | Primary trading pair symbol; runtime may fill this from holdings |
| `interval_seconds` | integer | `60`        | Main loop execution interval in seconds                          |
| `timeframe`        | string  | `15m`       | Candle timeframe                                                 |
| `read_only`        | boolean | `false`     | Read-only mode (no trades executed)                              |


---

## Trading Configuration

```yaml
trading:
  trading_pair: ""
  interval_seconds: 60
  timeframe: "15m"
  mode: "full_auto"
```


| Parameter          | Type    | Description                         |
| ------------------ | ------- | ----------------------------------- |
| `trading_pair`     | string  | Runtime pair override               |
| `interval_seconds` | integer | Main loop interval                  |
| `timeframe`        | string  | Primary timeframe                   |
| `mode`             | string  | `dry_run`, `semi_auto`, `full_auto` |


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

### Stop-loss / take-profit: three runtime paths

These are **not** a single unified percentage in all code paths:


| Path                    | Where                                                                              | What drives levels                                                                                                                                                                                                                                                                                                                                                                                                  |
| ----------------------- | ---------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **1. Strategy levels**  | Strategies such as `ScalpingStrategy`                                              | `strategies.<name>.stop_loss_pct` / `take_profit_pct` on the signal (for scalping, `main._apply_strategy_mode_profile` syncs from `strategy_mode.scalping`).                                                                                                                                                                                                                                                        |
| **2. ATR plan**         | `trading/signal_runtime.py` when the aggregated signal has no absolute SL/TP       | `RiskManager.calc_sl_tp_from_atr` — ATR distance × multiplier from `MODE_ATR_PROFILES` (per active mode) or `risk.atr_multiplier`. **Not** the same numbers as path 1 percentages.                                                                                                                                                                                                                                  |
| **3. Percent fallback** | Bootstrap held positions, manual CLI track (`resolve_effective_sl_tp_percentages`) | If `use_dynamic_sl_tp` is true: `sl_tp_percent_source_when_dynamic` — `volatility` uses `DEFAULT_SL_TP` by pair class; `risk_config` uses `risk.stop_loss_pct` / `risk.take_profit_pct`. `main._apply_strategy_mode_profile` sets `risk_config` for `strategy_mode` **scalping** and **trend_only** so path 3 matches those mode percentages. Standard mode keeps default `volatility` unless you override in YAML. |



| Parameter                           | Type    | Default      | Description                                                                                                                             |
| ----------------------------------- | ------- | ------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| `use_dynamic_sl_tp`                 | boolean | `true`       | When true, path 3 uses `sl_tp_percent_source_when_dynamic` (below); when false, path 3 always uses `stop_loss_pct` / `take_profit_pct`. |
| `sl_tp_percent_source_when_dynamic` | string  | `volatility` | `volatility`                                                                                                                            |
| `stop_loss_pct`                     | float   | (varies)     | Loss side % (stored negative after resolve); used for path 3 when applicable.                                                           |
| `take_profit_pct`                   | float   | (varies)     | Profit side % for path 3 when applicable.                                                                                               |


---

## Strategies Configuration

```yaml
strategy_mode:
  active: scalping
  scalping:
    primary_timeframe: "15m"
    confirm_timeframe: "15m"
    trend_timeframe: "1h"
    stop_loss_pct: 1.0
    take_profit_pct: 3.0
    position_timeout_minutes: 30
    bootstrap_position_timeout_hours: 24

strategies:
  enabled:
    - trend_following
    - mean_reversion
    - breakout
    - scalping
  min_confidence: 0.35
  min_strategies_agree: 2
```

`strategy_mode.scalping.position_timeout_minutes` ใช้กับ scalp entries ที่บอทเปิดเอง ส่วน `strategy_mode.scalping.bootstrap_position_timeout_hours` ใช้กับ bootstrap-held positions ที่ถูก import ตอน startup โดยจะพยายามใช้อายุถือจริงจาก persisted position, trade state, หรือ exchange history ก่อน fallback ไปที่เวลาที่เริ่ม manage ในรันนี้

TIME exits ยังผ่าน voluntary-exit profit gate เดิม (`execution.enforce_min_profit_gate_for_voluntary_exit` และ `execution.min_voluntary_exit_net_profit_pct`) ดังนั้น position ที่อายุเกินกำหนดแต่กำไรสุทธิต่ำกว่า threshold จะยังไม่ถูกบังคับปิด

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
    USDT: 20.0
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
    USDT: 20.0
    BTC: 40.0
    ETH: 40.0

backtesting:
  require_validation_before_live: false
```

---

## Binance Thailand alignment (`bot_config.yaml`)

These notes summarize how the **current** Python runtime uses `bot_config.yaml` when the exchange is **Binance Thailand** (`api.binance.th`, `BinanceThClient` in `config.py`).


| YAML area                                               | Actual runtime behavior                                                                                                                                                                                                                                                                                               |
| ------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Live orders**                                         | `**.env` `LIVE_TRADING=true`** (`config.py`) is required to arm real exchange orders. YAML `simulate_only: false` and `read_only: false` must align; unset `BOT_STARTUP_TEST_MODE`; keep `BOT_READ_ONLY` / `SIMULATE_ONLY` off. See `.env.example`.                                                                   |
| `api_keys.binance_`* / `bitkub_*`                       | **Binance credentials are not read from YAML.** Use `.env`: `BINANCE_API_KEY`, `BINANCE_API_SECRET`. Optional: `telegram_bot_token` / `telegram_chat_id` here or in `.env` / `notifications`. Legacy `bitkub_`* entries are unused.                                                                                   |
| `websocket`                                             | Enables runtime WebSocket pricing when a supported backend is available (native Binance stream in Binance mode). If unavailable, runtime falls back to REST pricing.                                                                                                                                                  |
| `balance_monitor`                                       | Uses whatever `api_client` the bot was constructed with (`BinanceThClient`). Balances refresh; **fiat/crypto deposit/withdraw history** calls are **stubbed empty** on Binance.th (`api_client.py`), so history-driven deposit/withdraw events usually do not fire.                                                   |
| `monitoring` / reconciliation                           | `monitoring.py` reconciles via `api_client` + executor — **not** Bitkub-specific.                                                                                                                                                                                                                                     |
| `rebalance`                                             | `portfolio_rebalancer.py` is exchange-agnostic; set `cash_assets` / `target_allocation` keys to match your **quote** asset (e.g. **USDT** for `*USDT` pairs).                                                                                                                                                         |
| `data.hybrid_dynamic_coin_config.min_quote_balance_thb` | **Legacy key name** (`_thb`); value is treated as a **minimum quote balance** threshold — for USDT pairs interpret as **USDT**.                                                                                                                                                                                       |
| `multi_timeframe.required_candles_for_readiness`        | Integer (default **35**, clamped **5–2000**). Each **gated** timeframe for a pair must have at least this many rows in `prices` before MTF readiness marks the pair `ready` (`trading/status_runtime.py` + `trading_bot._filter_pairs_by_candle_readiness`). Lower = faster startup, higher = more indicator history. |


---

## Validation

Configuration is validated by the runtime on startup. For operational checks, use:

```powershell
.\.venv-3\Scripts\python.exe scripts/vps_preflight.py --bot-health-url http://127.0.0.1:8080/health --json
```

