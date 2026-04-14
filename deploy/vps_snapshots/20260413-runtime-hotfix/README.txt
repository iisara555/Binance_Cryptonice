VPS runtime snapshot captured on 2026-04-13 from root@188.166.253.203:/root/Crypto_Sniper

Purpose:
- Preserve the currently running production hotfix files without overwriting local worktree files.
- Local main.py, trading_bot.py, and cli_ui.py had diverged from the VPS runtime and were missing the Rich dashboard hotfix signatures that are currently running healthy on the server.
- A direct deploy from the local worktree was intentionally skipped to avoid regressing production.

Files captured from VPS:
- main.py
- trading_bot.py
- cli_ui.py
- trade_executor.py

Observed VPS status at capture time:
- health endpoint returned healthy=true
- tmux session crypto was active
- Rich dashboard continued updating
