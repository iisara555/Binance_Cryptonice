#!/usr/bin/env bash
set -euo pipefail
# Run once on the VPS (as root): disable direct-python systemd bots, clear duplicates,
# keep a single bot inside tmux (session name from TMUX_SESSION_NAME, default crypto).
# Idempotent: safe to re-run after deploy.

ROOT="${CRYPTO_BOT_ROOT:-/root/Crypto_Sniper}"
SESSION_NAME="${TMUX_SESSION_NAME:-crypto}"
WRAPPER="${ROOT}/deploy/systemd/crypto-bot-tmux.sh"
ATTACH="${ROOT}/deploy/systemd/crypto-attach"

for u in crypto-sniper crypto-bot-runtime; do
    systemctl disable --now "${u}.service" 2>/dev/null || true
done

for legacy in sniper bot; do
    if [ "${legacy}" != "${SESSION_NAME}" ] && tmux has-session -t "${legacy}" 2>/dev/null; then
        echo "Removing legacy tmux session: ${legacy}"
        tmux kill-session -t "${legacy}"
    fi
done

if tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
    tmux kill-session -t "${SESSION_NAME}"
fi

sleep 1
echo "Stopping stray bot PIDs under ${ROOT} ..."
for pid in $(pgrep -f "main.py" 2>/dev/null || true); do
    if [ -r "/proc/${pid}/cwd" ] && [ "$(readlink "/proc/${pid}/cwd" 2>/dev/null)" = "${ROOT}" ]; then
        echo "  kill -9 ${pid}"
        kill -9 "${pid}" 2>/dev/null || true
    fi
done
sleep 2

chmod +x "${WRAPPER}" "${ATTACH}" "${ROOT}/deploy/systemd/vps_switch_to_tmux_only.sh" 2>/dev/null || true

cp -f "${ROOT}/deploy/systemd/crypto-bot-tmux.service" /etc/systemd/system/crypto-bot-tmux.service
systemctl daemon-reload
systemctl enable crypto-bot-tmux.service
# oneshot + RemainAfterExit: plain "start" does nothing if already active — force a fresh tmux.
systemctl restart crypto-bot-tmux.service

if [ ! -e /usr/local/bin/crypto-tmux ]; then
    ln -sf "${ATTACH}" /usr/local/bin/crypto-tmux
fi

echo "Single tmux runtime is active (session: ${SESSION_NAME})."
echo "  Attach: crypto-tmux"
echo "  Or:    tmux attach -t ${SESSION_NAME}"
