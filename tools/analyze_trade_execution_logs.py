#!/usr/bin/env python3
"""
Scan exported bot stdout/log files for execution-path diagnostics (POL/USDT/multi-pair).

Use on VPS logs, e.g.:
  python tools/analyze_trade_execution_logs.py path/to/bot.log
  python tools/analyze_trade_execution_logs.py bot.log --symbol POLUSDT --last 8000

Implements checklist from dashboard execution diagnosis:
PreTradeGate + failed_checks, Risk Manager risk_check reject, RiskGate / can_open_position,
confirmation gate, ATR plan build.

No network; read-only local file IO.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Sequence, Tuple

# Compiled section headers for grouped output order
_PATTERN_GROUPS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    (
        "PreTradeGate (blocked)",
        re.compile(r"\[PreTradeGate\]", re.I),
    ),
    (
        "failed_checks CSV",
        re.compile(r"failed_checks=", re.I),
    ),
    (
        "SignalRisk / aggregate risk reject",
        re.compile(r"Risk Manager:.*ปฏิเสธ|RiskCheck:Final.*REJECT|check_risk", re.I),
    ),
    (
        "RiskGate (can_open_position)",
        re.compile(r"\[RiskGate\]", re.I),
    ),
    (
        "Correlation gate",
        re.compile(r"\[CorrelationGate\]", re.I),
    ),
    (
        "Confirmation candles gate",
        re.compile(r"\[ConfirmationGate\]", re.I),
    ),
    (
        "ATR / execution plan blocked",
        re.compile(r"ATR unavailable|trade rejected.*ATR|Sizing rejected", re.I),
    ),
)


def _iter_bounded_lines(handle: Iterable[str], last_n: int | None) -> List[str]:
    lines = list(handle)
    if last_n is not None and len(lines) > last_n:
        return lines[-last_n:]
    return lines


def _symbol_predicate(symbol: str) -> Callable[[str], bool]:
    sym = symbol.strip().upper()
    if not sym:
        return lambda _ln: True
    needles = [sym]
    base = sym.replace("USDT", "").replace("THB_", "").strip()
    if base and len(base) >= 2:
        needles.append(base)
        needles.append(base + "/")
    needles_tpl = tuple(n.upper() for n in needles if n)

    def _matches(line: str) -> bool:
        uline = line.upper()
        return any(n in uline or n.replace("_", "") in uline.replace("_", "") for n in needles_tpl)

    return _matches


def scan_log_lines(lines: Sequence[str], sym_filter: Callable[[str], bool]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {title: [] for title, _ in _PATTERN_GROUPS}
    for line in lines:
        if not sym_filter(line):
            continue
        for title, pat in _PATTERN_GROUPS:
            if pat.search(line):
                out[title].append(line.rstrip())
    return out


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Grep diagnostic patterns for POL/execution troubleshooting.")
    p.add_argument("log_file", nargs="?", help="Plain-text log path (stdin if '-' or omitted)")
    p.add_argument(
        "--symbol",
        "-s",
        default="POLUSDT",
        help='Symbol substring to filter lines (default POLUSDT), or "ALL"',
    )
    p.add_argument(
        "--last",
        type=int,
        default=None,
        metavar="N",
        help="Only scan last N lines (tail) for huge files",
    )
    args = p.parse_args(list(argv) if argv is not None else None)

    sym = str(args.symbol or "POLUSDT").strip()
    filt = lambda ln: True if sym.upper() == "ALL" else _symbol_predicate(sym)(ln)

    if args.log_file in (None, "-"):
        raw = _iter_bounded_lines(sys.stdin, args.last)
    else:
        path = Path(args.log_file)
        if not path.is_file():
            print(f"No such file: {path}", file=sys.stderr)
            return 2
        with path.open(encoding="utf-8", errors="replace") as f:
            raw = _iter_bounded_lines(f.readlines(), args.last)

    sections = scan_log_lines(raw, filt)
    hits = sum(len(v) for v in sections.values())

    print(f"{'=' * 60}")
    print(f"Execution-path scan  symbol_filter={sym!r}  lines={len(raw)}  hits={hits}")
    print(f"Patterns: PreTradeGate, failed_checks=, Risk reject, RiskGate, CorrelationGate,")
    print(f"          ConfirmationGate, ATR/sizing.")
    print(f"{'=' * 60}\n")

    for title, pat in _PATTERN_GROUPS:
        bucket = sections.get(title, [])
        print(f"-- {title} ({len(bucket)} lines)")
        if not bucket:
            print(f"    (none matching {sym!r})")
        else:
            for ln in bucket[:200]:
                print(f"    {ln}")
            if len(bucket) > 200:
                print(f"    ... ({len(bucket) - 200} more)")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
