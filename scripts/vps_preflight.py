"""Run a VPS preflight checklist for the crypto bot deployment."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}

    values: Dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _env_value(key: str, env_file: Dict[str, str]) -> str:
    value = os.environ.get(key)
    if value is not None:
        return value.strip()
    return str(env_file.get(key, "") or "").strip()


def _looks_like_placeholder(value: str) -> bool:
    if not value:
        return True
    upper = value.upper()
    return any(marker in upper for marker in ("YOUR_", "REPLACE", "CHANGE_ME", "PLACEHOLDER"))


def _http_json(url: str, timeout: float) -> Dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset))


def _check(checks: List[Dict[str, Any]], name: str, ok: bool, detail: str, severity: str = "error") -> None:
    checks.append({
        "name": name,
        "ok": bool(ok),
        "severity": severity,
        "detail": detail,
    })


def run_preflight(
    project_root: Path,
    bot_health_url: Optional[str],
    timeout: float,
    allow_auth_degraded: bool,
    skip_http: bool,
) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []

    env_path = project_root / ".env"
    env_values = _load_env_file(env_path)
    _check(checks, ".env present", env_path.exists(), f"Expected root env file at {env_path}")

    binance_key = _env_value("BINANCE_API_KEY", env_values)
    binance_secret = _env_value("BINANCE_API_SECRET", env_values)
    _check(checks, "BINANCE_API_KEY configured", not _looks_like_placeholder(binance_key), "BINANCE_API_KEY must be set to a real value")
    _check(checks, "BINANCE_API_SECRET configured", not _looks_like_placeholder(binance_secret), "BINANCE_API_SECRET must be set to a real value")

    bot_config = project_root / "bot_config.yaml"
    _check(checks, "bot_config.yaml present", bot_config.exists(), f"Expected bot config at {bot_config}")

    db_path = _env_value("SQLITE_DB_PATH", env_values) or str(project_root / "crypto_bot.db")
    _check(checks, "SQLite database present", Path(db_path).exists(), f"Expected SQLite DB at {db_path}")

    if not skip_http:
        if bot_health_url:
            try:
                bot_health = _http_json(bot_health_url, timeout)
                healthy = bool(bot_health.get("healthy"))
                is_degraded = str(bot_health.get("status") or "").lower() == "degraded"

                if is_degraded:
                    detail = f"Bot health payload: {bot_health}"
                    if allow_auth_degraded:
                        _check(
                            checks,
                            "Bot health endpoint reachable",
                            healthy,
                            f"{detail} (auth-degraded explicitly allowed)",
                            severity="warning",
                        )
                    else:
                        _check(
                            checks,
                            "Bot health endpoint reachable",
                            False,
                            f"{detail} (auth-degraded is not allowed in strict preflight)",
                        )
                else:
                    _check(checks, "Bot health endpoint reachable", healthy, f"Bot health payload: {bot_health}")
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                _check(checks, "Bot health endpoint reachable", False, f"HTTP {exc.code}: {body}")
            except Exception as exc:
                _check(checks, "Bot health endpoint reachable", False, str(exc))

    errors = [item for item in checks if not item["ok"] and item["severity"] == "error"]
    warnings = [item for item in checks if not item["ok"] and item["severity"] == "warning"]
    passed = [item for item in checks if item["ok"]]

    return {
        "status": "pass" if not errors else "fail",
        "project_root": str(project_root),
        "checks": checks,
        "passed": len(passed),
        "warnings": len(warnings),
        "errors": len(errors),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run VPS preflight checks for the crypto bot deployment")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT), help="Project root path")
    parser.add_argument("--bot-health-url", default="http://127.0.0.1:8080/health", help="Bot health URL; pass empty string to skip")
    parser.add_argument("--skip-bot-health", action="store_true", help="Skip probing the bot health endpoint")
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout in seconds")
    parser.add_argument("--allow-auth-degraded", action="store_true", help="Treat auth-degraded bot health as a warning instead of a failure")
    parser.add_argument("--skip-http", action="store_true", help="Skip live HTTP checks and only validate local files/config")
    parser.add_argument("--json", action="store_true", help="Print JSON only")
    args = parser.parse_args()

    result = run_preflight(
        project_root=Path(args.project_root).resolve(),
        bot_health_url=None if args.skip_bot_health else (args.bot_health_url or None),
        timeout=args.timeout,
        allow_auth_degraded=args.allow_auth_degraded,
        skip_http=args.skip_http,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps(result, indent=2))
        if result["status"] == "pass":
            print("\nPreflight passed.")
        else:
            print("\nPreflight failed. Fix the failing checks before go-live.")

    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())