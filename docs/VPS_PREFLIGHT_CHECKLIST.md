# VPS Preflight Checklist

Use this checklist before enabling live trading on a VPS.

## 1. Files And Runtime

- Confirm the root `.env` exists and contains real `BITKUB_API_KEY` and `BITKUB_API_SECRET` values.
- Confirm `bot_config.yaml` exists and the `monitoring.health_check_port` / `monitoring.health_check_path` values match the port you intend to probe from the VPS.
- Confirm the SQLite database path exists locally on the VPS. By default this is `crypto_bot.db`, or `SQLITE_DB_PATH` if you override it.
- Install root requirements into the runtime Python environment.

## 2. Network And Exposure

- Keep the bot health endpoint private when possible.
- Probe the bot health server on `monitoring.health_check_port` only from trusted networks or a private load balancer.
- Add the VPS public IP to the Bitkub API allowlist before attempting live mode.

## 3. Start Order

- Start the trading bot with the project virtualenv, not the system Python.
- Start the trading bot with `BOT_READ_ONLY=true` or `BOT_STARTUP_TEST_MODE=1` for the first smoke test.
- If you want Rich CLI plus reboot-safe autostart, start the bot via a `tmux` session and let a oneshot `systemd` unit recreate that session on boot.
- Do not use ad-hoc shell state from an old project path after moving folders; reopen the shell and activate from the new root.

## 4. Health Checks

- Bot health: `GET http://127.0.0.1:8080/health` unless you changed `monitoring.health_check_port` or `monitoring.health_check_path`

The bot health endpoint returns `status=degraded` when Bitkub private auth is unavailable but the process stays alive in safe public-only mode. Treat that as a deployment warning, not a green light for live trading.

## 5. Dry-Run Smoke Test

Run this on the VPS after the runtime starts:

```bash
python scripts/vps_preflight.py \
  --bot-health-url http://127.0.0.1:8080/health
```

This strict form is the live-readiness gate. It must fail if the bot reports `status=degraded`.

For a first-pass deploy where Bitkub private auth is intentionally not ready yet, allow degraded mode explicitly:

```bash
python scripts/vps_preflight.py \
  --bot-health-url http://127.0.0.1:8080/health \
  --allow-auth-degraded
```

If you are only validating local files and config and have not started the bot process yet:

```bash
python scripts/vps_preflight.py --skip-bot-health
```

## 6. Go-Live Gate

- Preflight script returns `status: pass`
- Bot `/health` returns `healthy: true` and not `status: degraded`
- If using Rich CLI on VPS, `tmux list-sessions` shows the expected session (for example `crypto`) after boot
- Telegram behavior is intentional: valid credentials for alerts, or disabled by config/env
- `LIVE_TRADING` and `simulate_only` flags are reviewed one last time before restart
