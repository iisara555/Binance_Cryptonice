#!/usr/bin/env python3
"""
Local validation gate: bytecode compile check, optional YAML parse, pytest.

Usage (from repo root):
  python scripts/validate_project.py
  python scripts/validate_project.py --no-tests
"""

from __future__ import annotations

import argparse
import compileall
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# App + tests only (skip vendored exchange/ subtree — very large; edit there rarely)
_COMPILE_DIRS = ("tests", "trading", "strategies", "util", "scripts", "plugins")


def _compile() -> bool:
    ok = True
    for fp in sorted(PROJECT_ROOT.glob("*.py")):
        ok = bool(compileall.compile_file(str(fp), quiet=1)) and ok
    for sub in _COMPILE_DIRS:
        d = PROJECT_ROOT / sub
        if d.is_dir():
            ok = bool(compileall.compile_dir(str(d), quiet=1, maxlevels=25)) and ok
    if ok:
        print("[validate] compileall (app dirs + root *.py): OK")
    else:
        print("[validate] compileall reported failures", file=sys.stderr)
    return ok


def _yaml() -> bool:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        print("[validate] PyYAML not installed — skip bot_config.yaml parse")
        return True
    cfg = PROJECT_ROOT / "bot_config.yaml"
    if not cfg.exists():
        print("[validate] bot_config.yaml missing", file=sys.stderr)
        return False
    try:
        with open(cfg, encoding="utf-8") as f:
            yaml.safe_load(f)
    except Exception as exc:
        print(f"[validate] bot_config.yaml parse failed: {exc}", file=sys.stderr)
        return False
    print("[validate] bot_config.yaml: OK")
    return True


def _pytest() -> bool:
    rc = subprocess.call(
        [sys.executable, "-m", "pytest", "tests", "-q", "--tb=no"],
        cwd=str(PROJECT_ROOT),
    )
    if rc != 0:
        print("[validate] pytest exited with code %s" % rc, file=sys.stderr)
    return rc == 0


def main() -> int:
    p = argparse.ArgumentParser(description="Project validation (compile + yaml + pytest).")
    p.add_argument("--no-tests", action="store_true", help="Skip pytest")
    args = p.parse_args()

    ok = _compile() and _yaml()
    if ok and not args.no_tests:
        ok = _pytest()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
