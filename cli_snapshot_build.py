# Backward-compat shim — real module lives in cli/snapshot_build.py
from cli.snapshot_build import *  # noqa: F401,F403
from cli.snapshot_build import build_open_position_rows_for_cli, compute_cli_balance_websocket_health  # noqa: F401
