"""Unit tests for Telegram bot helpers (_resolve_sqlite_db_path, emergency kill order sweep)."""

import tempfile
from pathlib import Path
from unittest.mock import Mock

from telegram_bot import (
    _emergency_kill_collect_open_orders,
    _extract_cancel_fields,
    _flatten_exchange_open_orders,
    _resolve_sqlite_db_path,
)


def test_resolve_sqlite_db_path_absolute_unchanged():
    raw = str(Path(tempfile.gettempdir()).resolve() / "_tg_resolve_abs.db")
    assert Path(raw).is_absolute()
    p = _resolve_sqlite_db_path(Mock(), raw)
    assert Path(p).resolve() == Path(raw).resolve()


def test_resolve_sqlite_db_path_relative_to_config_parent():
    with tempfile.TemporaryDirectory() as td:
        conf_dir = Path(td) / "conf"
        conf_dir.mkdir()
        cfg = conf_dir / "bot_config.yaml"
        cfg.write_text("{}", encoding="utf-8")

        class App:
            _config_path = cfg
            config = {"database": {"db_path": "storage/app.db"}}

        out = _resolve_sqlite_db_path(App(), App.config["database"]["db_path"])
        assert Path(out).resolve() == (conf_dir / "storage" / "app.db").resolve()


def test_emergency_kill_collect_open_orders_uses_global_first():
    calls = []

    class API:
        def get_open_orders(self, symbol):
            calls.append(symbol)
            if symbol is None:
                return [
                    {"id": "a1", "symbol": "THB_BTC", "side": "BUY"},
                ]
            raise AssertionError("unexpected per-pair call when global succeeds")

    rows = _emergency_kill_collect_open_orders(API(), pairs_to_check=[])
    assert len(rows) == 1
    assert rows[0]["id"] == "a1"
    assert calls == [None]


def test_emergency_kill_collect_open_orders_dict_result_shape():
    class API:
        def get_open_orders(self, symbol):
            return {"result": [{"id": "x", "symbol": "THB_ETH", "side": "sell"}]}

    rows = _emergency_kill_collect_open_orders(API(), pairs_to_check=[])
    assert len(rows) == 1 and rows[0]["id"] == "x"


def test_emergency_kill_collect_falls_back_to_per_pair():
    calls = []

    class API:
        def get_open_orders(self, symbol):
            calls.append(symbol)
            if symbol is None:
                raise RuntimeError("global unsupported")
            if symbol == "THB_XRP":
                return [{"id": "99", "symbol": "THB_XRP", "side": "BUY"}]
            return []

    rows = _emergency_kill_collect_open_orders(API(), pairs_to_check=["THB_XRP", "THB_ETH"])
    assert len(rows) == 1 and rows[0]["id"] == "99"
    assert calls[0] is None


def test_emergency_kill_collect_empty_when_all_fail():
    class API:
        def get_open_orders(self, symbol):
            if symbol is None:
                raise RuntimeError("fail global")
            raise RuntimeError("fail pair")

    rows = _emergency_kill_collect_open_orders(API(), pairs_to_check=["THB_BTC"])
    assert rows == []


def test_flatten_exchange_open_orders_shapes():
    assert _flatten_exchange_open_orders(None) == []
    assert _flatten_exchange_open_orders([]) == []
    assert len(_flatten_exchange_open_orders({"result": [{"id": "1"}]})) == 1
    assert _flatten_exchange_open_orders({}) == []


def test_extract_cancel_fields_norm_and_raw_symbol():
    oid, pair, side = _extract_cancel_fields({"id": "99", "symbol": "THB_BTC", "side": "BUY"})
    assert (oid, pair, side) == ("99", "THB_BTC", "buy")
    oid2, pair2, side2 = _extract_cancel_fields({"id": "1", "_raw": {"symbol": "THB_ETH"}})
    assert pair2 == "THB_ETH" and side2 == "sell"
