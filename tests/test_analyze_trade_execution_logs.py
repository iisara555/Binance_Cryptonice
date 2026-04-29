"""Tests for tools/analyze_trade_execution_logs.py grouped pattern scan."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_logscan():
    path = ROOT / "tools" / "analyze_trade_execution_logs.py"
    spec = importlib.util.spec_from_file_location("analyze_trade_execution_logs", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_scan_log_lines_finds_pret_gate_pol() -> None:
    mod = _load_logscan()
    lines = [
        "noise",
        "[PreTradeGate] POLUSDT blocked: x | failed_checks=Portfolio above minimum",
        "[ConfirmationGate] ADAUSDT BUY signal pending",
    ]
    filt = mod._symbol_predicate("POLUSDT")
    sections = mod.scan_log_lines(lines, filt)
    assert any("PreTradeGate" in ln for ln in sections.get("PreTradeGate (blocked)", []))
    assert not sections.get("Confirmation candles gate")


def test_symbol_all_scans_all_matching_patterns() -> None:
    mod = _load_logscan()
    lines = [
        "[RiskGate] Trade blocked for BTCUSDT: Cooldown",
        "[PreTradeGate] ETHUSDT blocked: y | failed_checks=X",
    ]
    sections = mod.scan_log_lines(lines, lambda _ln: True)
    assert sections["RiskGate (can_open_position)"]
    assert sections["PreTradeGate (blocked)"]
