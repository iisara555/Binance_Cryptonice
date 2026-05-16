# Backward-compat shim — real module lives in cli/ui.py
from cli.ui import *  # noqa: F401,F403
from cli.ui import CLICommandCenter  # noqa: F401
