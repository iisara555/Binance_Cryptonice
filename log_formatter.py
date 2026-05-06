"""CryptoBot V1 โ€” compact, scannable log formatter.

Format:  HH:MM:SS โ” LEVEL โ” TAG  โ” EMOJI  message

NOTE: not placed under logging/ to avoid shadowing the stdlib logging package.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# โ”€โ”€ Tag map: module leaf name โ’ 4-char tag โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€
TAG_MAP: Dict[str, str] = {
    "pre_trade_gate_runtime": "GATE",
    "risk_management":        "RISK",
    "signal_generator":       "SIG ",
    "trade_executor":         "EXEC",
    "data_collector":         "DATA",
    "signal_runtime":         "FLOW",
    "position_monitor":       "POS ",
    "startup_runtime":        "BOOT",
    "api_client":             "API ",
    "state_management":       "STM ",
    "execution_runtime":      "RUN ",
    "managed_lifecycle":      "OMS ",
    "position_bootstrap":     "BSTP",
    "risk_volatility":        "RVOL",
    "balance_monitor":        "BAL ",
    "multi_timeframe":        "MTF ",
    "trading_bot":            "BOT ",
    "portfolio_manager":      "PORT",
    "signal_pipeline":        "PIPE",
}

# โ”€โ”€ Level badges โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€
LEVEL_BADGE: Dict[str, str] = {
    "DEBUG":    "DBG ",
    "INFO":     "INFO",
    "WARNING":  "WARN",
    "ERROR":    "ERR ",
    "CRITICAL": "CRIT",
}

# โ”€โ”€ ANSI colors for non-Rich console output โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€
_ANSI_RESET  = "\033[0m"
_ANSI_DIM    = "\033[2m"
_ANSI_BOLD   = "\033[1m"
_ANSI_WHITE  = "\033[97m"
_ANSI_YELLOW = "\033[93m"
_ANSI_RED    = "\033[91m"
_ANSI_CYAN   = "\033[96m"
_ANSI_GREEN  = "\033[92m"

_LEVEL_ANSI: Dict[str, str] = {
    "DBG ": _ANSI_DIM,
    "INFO": _ANSI_WHITE,
    "WARN": _ANSI_YELLOW,
    "ERR ": f"{_ANSI_BOLD}{_ANSI_RED}",
    "CRIT": f"{_ANSI_BOLD}{_ANSI_RED}",
}

# โ”€โ”€ Verbose prefix patterns to strip โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€
_VERBOSE_PREFIX_RE = re.compile(
    r"^\s*\["
    r"(?:PreTradeGate|SIGNAL_FLOW|Trade Decision|Trade Triggered"
    r"|State Machine|Bootstrap Positions|BUY|SELL)"
    r"\]\s*",
    re.IGNORECASE,
)

# โ”€โ”€ Symbol normaliser โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€
_STRIP_SUFFIXES = ("USDT", "BUSD", "BTC", "ETH", "BNB", "BNB")


def shorten_symbol(pair: str) -> str:
    """BTCUSDT โ’ BTC, DOGEUSDT โ’ DOGE."""
    up = pair.upper().strip("'\"[] \t")
    for sfx in _STRIP_SUFFIXES:
        if up.endswith(sfx) and len(up) > len(sfx):
            return up[: -len(sfx)]
    return up


def _extract_symbol(text: str) -> Optional[str]:
    m = re.search(r"\b([A-Z]{2,8}(?:USDT|BUSD|BTC|ETH|BNB))\b", text.upper())
    return shorten_symbol(m.group(1)) if m else None


# โ”€โ”€ Emoji picker โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€

def pick_emoji(tag: str, msg_lower: str) -> str:  # noqa: C901
    """Return the best-fit emoji for this tag+message combination."""
    tag = tag.strip()

    # Trade lifecycle โ€” check first (highest priority)
    if "trade opened" in msg_lower or (
        ("opened" in msg_lower or "order filled" in msg_lower or "order placed" in msg_lower)
        and "buy" in msg_lower
    ):
        return "โ…"
    if "closed" in msg_lower or "close" in msg_lower:
        if any(w in msg_lower for w in ("sl", "stop loss", "stop_loss")):
            return "๐‘"
        if any(w in msg_lower for w in ("tp", "take profit", "take_profit")) or "+" in msg_lower:
            return "๐’ฐ"
        if any(w in msg_lower for w in ("time", "held", "timeout")):
            return "โฐ"
        if any(w in msg_lower for w in ("loss", "-")):
            return "๐“"
        return "๐’ฐ"  # default closed = profit assumed

    # Risk / system alerts
    if "daily limit" in msg_lower and "hit" in msg_lower:
        return "๐จ"
    if "paused" in msg_lower or "mismatch" in msg_lower:
        return "โ”"
    if "daily loss" in msg_lower or ("warn" in msg_lower and tag == "RISK"):
        return "โ ๏ธ"
    if "daily reset" in msg_lower or "reset" in msg_lower and tag == "RISK":
        return "๐’น"
    if "cooldown" in msg_lower or "cooling" in msg_lower:
        return "๐’ค"

    # Connectivity
    if ("websocket" in msg_lower or "ws" in msg_lower) and "connect" in msg_lower:
        if "disconnect" in msg_lower or "reconnect" in msg_lower or "closed" in msg_lower:
            return "๐”ด"
        return "๐”"

    # Boot / startup
    if tag == "BOOT":
        if "started" in msg_lower or "bot start" in msg_lower:
            return "๐€"
        if "bootstrap" in msg_lower or "wallet" in msg_lower:
            return "๐“ฆ"
        if "stale" in msg_lower:
            return "๐—‘๏ธ"
        if "scanning" in msg_lower:
            return "๐”"

    # Data
    if "up to date" in msg_lower or "uptodate" in msg_lower:
        return "๐”"
    if ("candle" in msg_lower and "stored" in msg_lower) or ("new candle" in msg_lower):
        return "๐“ก"
    if "backfill" in msg_lower or "historical" in msg_lower:
        return "๐“ฅ"
    if any(w in msg_lower for w in ("exchangeinfo", "bulk cache", "filters loaded", "filter cache")):
        return "๐"
    if "reconcil" in msg_lower:
        return "๐”"
    if "retry" in msg_lower or "retrying" in msg_lower:
        return "๐”"

    # Signal
    if tag == "SIG " or "signal" in msg_lower:
        conf_m = re.search(r"conf[=:]?\s*([\d.]+)", msg_lower)
        if conf_m:
            try:
                if float(conf_m.group(1)) >= 0.90:
                    return "๐ฏ"
            except ValueError:
                pass
        if "block" in msg_lower or "reject" in msg_lower or "fail" in msg_lower:
            return "โ"
        return "๐“"

    # State transitions
    if "โ’" in msg_lower or ("transition" in msg_lower and tag == "STM "):
        return "๐”"

    # Orders
    if "order" in msg_lower:
        if "sent" in msg_lower or "placed" in msg_lower or "submit" in msg_lower:
            return "๐“ค"
        if "filled" in msg_lower:
            return "๐“ฉ"
        if "error" in msg_lower or "failed" in msg_lower:
            return "โ—"

    # Errors
    if any(w in msg_lower for w in ("error", "failed", "exception", "traceback")):
        return "โ—"
    if "warning" in msg_lower:
        return "โ ๏ธ"

    # DB
    if any(w in msg_lower for w in ("database", "db write", "saved to db")):
        return "๐’พ"

    # Size / risk calc
    if tag == "RISK" and any(w in msg_lower for w in ("size", "portfolio", "nav", "risk_pct")):
        return "๐ก๏ธ"

    # Flow / trade decision
    if tag == "FLOW":
        if "triggered" in msg_lower:
            return "โ…"
        if "gated" in msg_lower:
            return "โณ"

    return ""


# โ”€โ”€ Strategy badge helper โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€

def _strategy_badge(text: str) -> str:
    lo = text.lower()
    if re.search(r"machete|mach|m8b|m8", lo):
        return "ใ€”Mใ€•"
    if re.search(r"scalp|simple_scalp|scalp_plus", lo):
        return "ใ€”Sใ€•"
    if re.search(r"breakout|bk", lo):
        return "ใ€”Bใ€•"
    return ""


# โ”€โ”€ Per-component message shortener โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€

def shorten_message(tag: str, msg: str) -> str:  # noqa: C901
    """Reformat a verbose log message to a compact, scannable string."""
    # Preserve original for side/direction detection before prefix stripping
    orig_lower = msg.lower()
    # Strip verbose prefixes from all messages
    msg = _VERBOSE_PREFIX_RE.sub("", msg).strip()
    # Collapse newlines
    msg = re.sub(r"\s*\n\s*", "  ", msg)

    lo = msg.lower()
    tag_bare = tag.strip()

    # โ”€โ”€ DATA โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€
    if tag_bare == "DATA":
        # stored N new candle(s)
        m = re.match(
            r"\[?([A-Z]{2,8}(?:USDT|BUSD|BTC)?)\]?\s+(\S+?):?\s+stored\s+(\d+)\s+new\s+candle",
            msg, re.I,
        )
        if m:
            return f"{shorten_symbol(m.group(1))}  {m.group(2).rstrip(':')}  +{m.group(3)} candle"

        # up to date
        m = re.match(
            r"\[?([A-Z]{2,8}(?:USDT|BUSD|BTC)?)\]?\s+(\S+?):?\s+.*up.?to.?date",
            msg, re.I,
        )
        if m:
            return f"{shorten_symbol(m.group(1))}  {m.group(2).rstrip(':')}  up to date"

        # pass-through grouped message (already shortened by CLI)
        if "up to date" in lo:
            return msg

        # backfill
        m = re.search(r"backfill.*?(\d+)\s+bars?", lo)
        if m:
            sym = _extract_symbol(msg) or ""
            tf_m = re.search(r"(\d+[mhd])", lo)
            tf = tf_m.group(1) if tf_m else ""
            return f"{sym}  {tf}  backfill {m.group(1)} bars"

    # โ”€โ”€ SIG โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€
    elif tag_bare == "SIG":
        sym = _extract_symbol(msg) or ""
        badge = _strategy_badge(msg)

        # Determine signal type
        sig_type = ""
        t_m = re.search(r"\btype\s*=\s*(\w+)", lo)
        if t_m:
            sig_type = t_m.group(1).upper()
        elif re.search(r"\bbuy\b", lo):
            sig_type = "BUY"
        elif re.search(r"\bsell\b", lo):
            sig_type = "SELL"

        conf_m = re.search(r"conf[=:]?\s*([\d.]+)", lo)
        conf = f"  conf={conf_m.group(1)}" if conf_m else ""

        rr_m = re.search(r"\brr[=:]?\s*([\d.]+)", lo)
        rr = ""
        if rr_m:
            try:
                rr = f"  rr={float(rr_m.group(1)):.2f}"
            except ValueError:
                pass

        # BLOCK / FAIL
        if re.search(r"\b(block|fail|reject|insuff)\b", lo):
            reason = ""
            r_m = re.search(r"reason\s*:\s*(.{0,40})", msg, re.I)
            if r_m:
                reason = f"  {r_m.group(1).strip()[:30]}"
            elif re.search(r"\(\d+/\d+\)", msg):
                # INSUFF (3/5) style
                cnt_m = re.search(r"\((\d+/\d+)\)", msg)
                reason = f"  INSUFF ({cnt_m.group(1)})" if cnt_m else ""
            return f"โ {sym}  {badge} BLOCK{reason}"

        if sym and sig_type:
            return f"{sym}  {badge} {sig_type}{conf}{rr}"

    # โ”€โ”€ GATE โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€
    elif tag_bare == "GATE":
        sym = _extract_symbol(msg) or ""

        # Blocked
        if re.search(r"\b(blocked|block|failed_check)\b", lo):
            # Try to extract reason
            reason = ""
            for pat in (
                r"failed[_\s]*checks?\s*[=:]\s*(.{0,40})",
                r"BLOCKED\s*[โ€”โ€“-]\s*failed:\s*(.{0,40})",
                r"failed:\s*(.{0,40})",
            ):
                r_m = re.search(pat, msg, re.I)
                if r_m:
                    reason = r_m.group(1).strip()[:35]
                    break

            # Extract pct if present
            pct_m = re.search(r"([\d.]+%\s*[<>]\s*[\d.]+%)", msg)
            if pct_m:
                reason = pct_m.group(1)

            return f"โ {sym}  BLOCK  {reason}".rstrip()

        # sizing preview / ready
        if re.search(r"sizing_preview|sizing_allowed|size.*preview", lo):
            side = "BUY" if "buy" in lo else "SELL"
            amt_m = re.search(r"quote_est\s*[=:]\s*([\d.]+)", lo)
            amt = amt_m.group(1) if amt_m else ""
            allowed = re.search(r"sizing_allowed\s*=\s*true", lo)
            mark = "โ…" if allowed else "โ"
            status = "ready" if allowed else "blocked"
            return f"{mark} {sym}  {side}  {amt} USDT  {status}"

    # โ”€โ”€ RISK โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€
    elif tag_bare == "RISK":
        # Position size line
        pv_m  = re.search(r"portfolio\s*[=:]\s*([\d.]+)", lo)
        rp_m  = re.search(r"risk_pct\s*[=:]\s*([\d.%]+)", lo)
        sz_m  = re.search(r"suggested\s*[=:]\s*([\d.]+)", lo)
        if pv_m and rp_m and sz_m:
            rp = rp_m.group(1).rstrip("%")
            return f"๐ก๏ธ size={sz_m.group(1)}  risk={rp}%  nav={pv_m.group(1)}  โ“"

        # Daily limit hit
        if "daily limit" in lo:
            return "๐จ daily limit hit  trading stopped"

        # Daily loss warning
        if "daily loss" in lo or "daily_loss" in lo:
            loss_m = re.search(r"daily_loss\s*[=:]\s*([\d.]+)", lo)
            max_m  = re.search(r"daily_loss_max\s*[=:]\s*([\d.]+)", lo)
            if loss_m and max_m:
                try:
                    pct = float(loss_m.group(1)) / float(max_m.group(1)) * 100
                    return f"โ ๏ธ daily loss={loss_m.group(1)}/{max_m.group(1)}  {pct:.0f}% used"
                except (ValueError, ZeroDivisionError):
                    pass

        # Daily reset
        if "daily reset" in lo or "loss reset" in lo:
            return "๐’น daily reset  loss cleared"

        # Cooldown
        if "cooldown" in lo or "cooling" in lo:
            min_m = re.search(r"(\d+)\s*min", lo)
            wait = f"  next trade in {min_m.group(1)}min" if min_m else ""
            return f"๐’ค cooldown{wait}"

    # โ”€โ”€ EXEC โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€
    elif tag_bare == "EXEC":
        sym = _extract_symbol(msg) or ""

        # Order details block
        if re.search(r"order details|usdt amount|coin:", lo):
            side = "BUY" if "buy" in orig_lower else "SELL"
            amt_m   = re.search(r"usdt amount\s*[:\s]*([\d.]+)", lo)
            price_m = re.search(r"price\s*[:\s]*([\d.]+)", lo)
            type_m  = re.search(r"\btype\s*[:\s]*(\w+)", lo)
            amt   = amt_m.group(1)   if amt_m   else ""
            price = price_m.group(1) if price_m else ""
            otype = type_m.group(1).upper() if type_m else ""
            return f"๐“ค {sym}  {side}  {amt} USDT @ {price}  {otype}".strip()

        # Error / failure
        err_m = re.search(r"\[(-\d+)\]\s*(.{0,60})", msg)
        if err_m:
            # Shorten pair names within error text
            err_text = re.sub(
                r"([A-Z]{2,8}USDT)",
                lambda m2: shorten_symbol(m2.group(1)),
                err_m.group(2).strip(),
            )[:45]
            return f"โ— {sym}  {err_m.group(1)} {err_text}"

        if re.search(r"placement error|order error|failed", lo):
            return f"โ— {sym}  " + msg[:50]

        # Retry
        if re.search(r"\bretry\b|\bretrying\b", lo):
            cnt_m = re.search(r"(\d+)[/\\](\d+)", lo)
            cnt   = f"  {cnt_m.group(1)}/{cnt_m.group(2)}" if cnt_m else ""
            return f"๐” {sym}  retry{cnt}"

        # Filled / placed / opened
        if re.search(r"order filled|order placed|order sent|buy order|sell order", lo):
            qty_m = re.search(r"qty\s*[=:]?\s*([\d.]+)", lo)
            qty = f"  qty={qty_m.group(1)}" if qty_m else ""
            side = "BUY" if "buy" in lo else "SELL"
            return f"โ… {sym}  {side} filled{qty}"

    # โ”€โ”€ FLOW โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€
    elif tag_bare == "FLOW":
        sym = _extract_symbol(msg) or ""

        # Trade triggered
        if re.search(r"triggered|trade triggered", lo):
            side  = "BUY" if "buy" in orig_lower else "SELL"
            badge = _strategy_badge(msg)
            pct_m = re.search(r"(\d+)%", msg)
            pct   = f" {pct_m.group(1)}%" if pct_m else ""
            score_m = re.search(r"score[:\s]*([\d]+)%?", lo)
            score = f"  score={score_m.group(1)}" if score_m else ""
            market_m = re.search(r"market[:\s]*(\w+)", lo)
            market = f"  {market_m.group(1).lower()}" if market_m else ""
            sym_part = f"  {sym}" if sym else ""
            return f"TRIGGERED{sym_part}  {side}  {badge}{pct}{score}{market}".strip()

        # Gated
        if "gated by" in lo:
            state_m = re.search(r"execution state[:\s]*(\w+)", lo)
            state = state_m.group(1).upper() if state_m else "GATED"
            return f"{sym}  gated  {state}"

        # Closed trades
        if "closed" in lo:
            price_m = re.search(r"@\s*([\d.]+)", msg)
            price = f" @ {price_m.group(1)}" if price_m else ""
            pct_m = re.search(r"([+-][\d.]+)%", msg)
            pct   = f"  {pct_m.group(1)}%" if pct_m else ""
            if re.search(r"\bsl\b|stop.?loss", lo):
                return f"๐‘ CLOSED  {sym}  SL{price}{pct}"
            if re.search(r"\btp\b|take.?profit", lo):
                return f"๐’ฐ CLOSED  {sym}  SELL{price}{pct}  TP"
            if "time" in lo or "held" in lo:
                held_m = re.search(r"held[=:\s]*([\d]+\s*min)", lo)
                held = f"  held={held_m.group(1)}" if held_m else ""
                return f"โฐ CLOSED  {sym}  TIME{held}{pct}"
            return f"๐“ CLOSED  {sym}  SELL{price}{pct}"

        # Opened (from trade decision logs)
        if "opened" in lo:
            side    = "BUY" if "buy" in lo else "SELL"
            amt_m   = re.search(r"([\d.]+)\s*usdt", lo)
            amt     = f"  {amt_m.group(1)} USDT" if amt_m else ""
            price_m = re.search(r"@\s*([\d.]+)", msg)
            price   = f" @ {price_m.group(1)}" if price_m else ""
            badge   = _strategy_badge(msg)
            return f"โ… OPENED  {sym}  {side}{amt}{price}  {badge}".strip()

    # โ”€โ”€ BOOT โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€
    elif tag_bare == "BOOT":
        # Bootstrap positions from wallet
        if re.search(r"bootstrap|wallet.*holding|holding.*add", lo):
            # extract list like ['ETHUSDT', 'BTCUSDT']
            list_m = re.search(r"\[([^\]]+)\]", msg)
            if list_m:
                raw_syms = [s.strip().strip("'\"") for s in list_m.group(1).split(",")]
                short_syms = " ".join(shorten_symbol(s) for s in raw_syms[:5])
                return f"๐“ฆ {short_syms}  added from wallet"
            return "๐“ฆ bootstrapped from wallet"

        if re.search(r"bot.?started|starting", lo):
            pairs_m = re.search(r"(\d+)\s+pair", lo)
            pairs   = f"  {pairs_m.group(1)} pairs" if pairs_m else ""
            return f"๐€ bot started{pairs}"

        if "stale" in lo:
            sym = _extract_symbol(msg) or ""
            return f"๐—‘๏ธ {sym}  stale position removed"

        if "scanning" in lo and "wallet" in lo:
            n_m = re.search(r"(\d+)\s+asset", lo)
            n   = f"  {n_m.group(1)} assets found" if n_m else ""
            return f"๐” scanning wallet{n}"

    # โ”€โ”€ OMS โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€
    elif tag_bare == "OMS":
        sym = _extract_symbol(msg) or ""

        if "paused" in lo or "mismatch" in lo:
            bot_m = re.search(r"bot[=:](\d+)", lo)
            ex_m  = re.search(r"exchange[=:](\d+)", lo)
            if bot_m and ex_m:
                return f"โ” PAUSED  mismatch bot={bot_m.group(1)} exchange={ex_m.group(1)}"
            return f"โ” PAUSED  {msg[:50]}"

        if "reconcil" in lo:
            cnt_m = re.search(r"(\d+)\s+ghost", lo)
            cnt   = f"  {cnt_m.group(1)} ghost orders cleared" if cnt_m else ""
            return f"๐” reconciled{cnt}"

        if re.search(r"failed|error", lo):
            att_m = re.search(r"(\d+)\s+attempt", lo)
            err_m = re.search(r"\[(-\d+)\]\s*(.{0,50})", msg)
            attempts = f" failed {att_m.group(1)}x" if att_m else ""
            if err_m:
                err_code = err_m.group(1)
                err_text = re.sub(
                    r"([A-Z]{2,8}USDT)",
                    lambda m2: shorten_symbol(m2.group(1)),
                    err_m.group(2).strip(),
                )[:30]
                code = f"  {err_code} {err_text}"
            else:
                code = ""
            return f"โ— {sym}{attempts}{code}"

    # โ”€โ”€ API โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€
    elif tag_bare == "API":
        if re.search(r"exchangeinfo|bulk cache|filters loaded|filter cache", lo):
            cnt_m = re.search(r"(\d+)\s+symbols?", lo)
            cnt   = f"  {cnt_m.group(1)} symbols" if cnt_m else ""
            ttl_m = re.search(r"ttl[=:]\s*([\d.]+)s", lo)
            ttl   = ""
            if ttl_m:
                hours = int(float(ttl_m.group(1))) // 3600
                ttl   = f"  ttl={hours}h" if hours else f"  ttl={ttl_m.group(1)}s"
            return f"๐ filters loaded{cnt}{ttl}"

        if "websocket" in lo and "connect" in lo and "disconnect" not in lo:
            pairs = re.findall(r"[A-Z]{2,8}USDT", msg.upper())
            pairs_str = " ".join(pairs[:4]) if pairs else ""
            return f"๐” websocket connected  {pairs_str}".strip()

        if "websocket" in lo and "disconnect" in lo:
            return "๐”ด websocket disconnected  reconnecting"

    # โ”€โ”€ STM โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€
    elif tag_bare == "STM":
        sym = _extract_symbol(msg) or ""

        # State transition โ€” handle both โ’ and -> and 'to'
        trans_m = re.search(r"(\w+)\s*(?:โ’|->|โ€“>)\s*(\w+)", msg)
        if not trans_m:
            trans_m = re.search(r"transition\s+(\w+)\s+to\s+(\w+)", msg, re.I)
        if trans_m:
            return f"๐” {sym}  {trans_m.group(1).upper()} โ’ {trans_m.group(2).upper()}"

    # โ”€โ”€ Fallback โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€
    return msg


def get_tag(logger_name: str) -> str:
    """Map a dotted logger name to a 4-char tag (with trailing space for alignment)."""
    if not logger_name:
        return "CORE"
    # Check the full name
    if logger_name in TAG_MAP:
        return TAG_MAP[logger_name]
    # Check the leaf (after last dot)
    tail = logger_name.rsplit(".", 1)[-1]
    if tail in TAG_MAP:
        return TAG_MAP[tail]
    # Fall back to first 4 chars of leaf
    return tail[:4].upper().ljust(4)[:4]


def format_log_row(record: logging.LogRecord) -> Dict[str, str]:
    """Return a dict suitable for the CLI log panel ring buffer.

    Keys: timestamp, level, tag, message, emoji
    """
    ts    = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
    level = record.levelname.upper()
    tag   = get_tag(record.name)
    raw   = record.getMessage().replace("\n", "  ").strip()
    if len(raw) > 200:
        raw = raw[:197] + "..."

    short = shorten_message(tag, raw)
    # Don't double-add emoji if shorten_message already prefixed it
    emoji_char = pick_emoji(tag, short.lower())

    return {
        "timestamp": ts,
        "level":     level,
        "tag":       tag,
        "message":   short,
        "emoji":     emoji_char,
    }


# โ”€โ”€ Console formatter โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€

class CryptoBotFormatter(logging.Formatter):
    """Compact formatter: HH:MM:SS โ” LEVEL โ” TAG  โ” EMOJI  message"""

    def __init__(self, use_color: bool = True) -> None:
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        ts    = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
        badge = LEVEL_BADGE.get(record.levelname, record.levelname[:4].upper())
        tag   = get_tag(record.name)
        tag_p = tag[:4].ljust(4)

        raw   = record.getMessage()
        short = shorten_message(tag_p.strip(), raw)
        emoji = pick_emoji(tag_p.strip(), short.lower())
        if emoji and not short.startswith(emoji):
            short = f"{emoji} {short}"

        if self.use_color:
            color = _LEVEL_ANSI.get(badge, _ANSI_WHITE)
            sep   = f"{_ANSI_DIM} โ” {_ANSI_RESET}"
            line  = (
                f"{_ANSI_DIM}{ts}{_ANSI_RESET}{sep}"
                f"{color}{badge}{_ANSI_RESET}{sep}"
                f"{_ANSI_CYAN}{tag_p}{_ANSI_RESET}{sep}"
                f"{short}"
            )
        else:
            line = f"{ts} โ” {badge} โ” {tag_p} โ” {short}"

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)

        return line
