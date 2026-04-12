#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="${TMUX_SESSION_NAME:-crypto}"
PROJECT_ROOT="${CRYPTO_BOT_ROOT:-/opt/crypto-bot-v1}"
PYTHON_BIN="${CRYPTO_BOT_PYTHON:-${PROJECT_ROOT}/.venv-3/bin/python}"
BOT_ENTRY="${CRYPTO_BOT_ENTRY:-${PROJECT_ROOT}/main.py}"

start_session() {
    if tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
        echo "tmux session '${SESSION_NAME}' already running"
        return 0
    fi

    tmux new-session -d -s "${SESSION_NAME}" -x 200 -y 50 -c "${PROJECT_ROOT}" "bash -lc 'stty -echo; exec \"${PYTHON_BIN}\" \"${BOT_ENTRY}\"'"
    echo "started tmux session '${SESSION_NAME}'"
}

stop_session() {
    if ! tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
        echo "tmux session '${SESSION_NAME}' is not running"
        return 0
    fi

    tmux kill-session -t "${SESSION_NAME}"
    echo "stopped tmux session '${SESSION_NAME}'"
}

attach_session() {
    exec tmux attach-session -t "${SESSION_NAME}"
}

status_session() {
    if tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
        tmux list-sessions | grep "^${SESSION_NAME}:"
        return 0
    fi

    echo "tmux session '${SESSION_NAME}' is not running"
    return 1
}

case "${1:-start}" in
    start)
        start_session
        ;;
    stop)
        stop_session
        ;;
    restart)
        stop_session
        start_session
        ;;
    attach)
        attach_session
        ;;
    status)
        status_session
        ;;
    *)
        echo "usage: $0 {start|stop|restart|attach|status}" >&2
        exit 2
        ;;
esac