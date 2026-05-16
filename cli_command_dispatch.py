# Backward-compat shim — real module lives in cli/command_dispatch.py
from cli.command_dispatch import *  # noqa: F401,F403
from cli.command_dispatch import CliCommandDispatcher  # noqa: F401
