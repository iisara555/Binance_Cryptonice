#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="${TMUX_SESSION_NAME:-crypto}"
PROJECT_ROOT="${CRYPTO_BOT_ROOT:-/root/Crypto_Sniper}"
PYTHON_BIN="${CRYPTO_BOT_PYTHON:-${PROJECT_ROOT}/.venv/bin/python}"
BOT_ENTRY="${CRYPTO_BOT_ENTRY:-${PROJECT_ROOT}/main.py}"
RESTART_DELAY="${CRYPTO_BOT_RESTART_DELAY:-5}"

escape_regex() {
    printf '%s' "$1" | sed 's/[][(){}.^$+*?|\\/]/\\&/g'
}

resolve_python_bin() {
    if [ -n "${CRYPTO_BOT_PYTHON:-}" ] && [ -x "${CRYPTO_BOT_PYTHON}" ]; then
        printf '%s\n' "${CRYPTO_BOT_PYTHON}"
        return 0
    fi

    for candidate in \
        "${PROJECT_ROOT}/.venv/bin/python" \
        "${PROJECT_ROOT}/.venv-3/bin/python" \
        "${PROJECT_ROOT}/venv/bin/python"
    do
        if [ -x "${candidate}" ]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done

    echo "could not locate a project Python executable under ${PROJECT_ROOT}" >&2
    return 1
}

is_bot_running() {
    local python_bin bot_entry pattern

    python_bin="$(resolve_python_bin)"
    bot_entry="${CRYPTO_BOT_ENTRY:-${PROJECT_ROOT}/main.py}"
    pattern="^$(escape_regex "${python_bin}") $(escape_regex "${bot_entry}")([[:space:]]|
|$)"

    pgrep -f -- "${pattern}" >/dev/null 2>&1
}

build_tmux_runner_command() {
    cat <<EOF
bash -lc 'stty -echo; trap "exit 0" TERM; while true; do "${PYTHON_BIN}" "${BOT_ENTRY}"; exit_code=\$?; if [ "\${exit_code}" -eq 130 ] || [ "\${exit_code}" -eq 143 ]; then printf "\\n[%s] bot stopped with exit code %s; not restarting.\\n" "\$(date -Is)" "\${exit_code}"; exit 0; fi; printf "\\n[%s] bot exited with code %s; restarting in %ss...\\n" "\$(date -Is)" "\${exit_code}" "${RESTART_DELAY}"; sleep "${RESTART_DELAY}"; done'
EOF
}

start_session() {
    PYTHON_BIN="$(resolve_python_bin)"

    if tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
        if is_bot_running; then
            echo "tmux session '${SESSION_NAME}' already running"
            return 0
        fi

        echo "tmux session '${SESSION_NAME}' exists but bot is not running; recreating session"
        tmux kill-session -t "${SESSION_NAME}"
    fi

    tmux new-session -d -s "${SESSION_NAME}" -x 200 -y 50 -c "${PROJECT_ROOT}" "$(build_tmux_runner_command)"
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
    if tmux has-session -t "${SESSION_NAME}" 2>/dev/null && is_bot_running; then
        tmux list-sessions | grep "^${SESSION_NAME}:"
        return 0
    fi

    if tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
        echo "tmux session '${SESSION_NAME}' exists but bot is not running"
        return 1
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