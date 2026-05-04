"""CLI footer-chat command routing for ``TradingBotApp``."""

from __future__ import annotations

from typing import Any, List


class CliCommandDispatcher:
    """Maps command names to ``TradingBotApp`` orchestration methods."""

    __slots__ = ("_app",)

    def __init__(self, app: Any) -> None:
        self._app = app

    @staticmethod
    def format_help() -> str:
        return (
            "Commands:\n"
            "  help\n"
            "  status\n"
            "  orders\n"
            "  mode show\n"
            "  mode set <standard|trend_only|scalping>\n"
            "  mode set <standard|trend_only|scalping> restart\n"
            "  confirm\n"
            "  cancel\n"
            "  risk show\n"
            "  risk set <percent>\n"
            "  ui\n"
            "  ui log <debug|info|warning|error|critical>\n"
            "  ui footer <compact|verbose>\n"
            "  sigflow | sf | s   (toggle full SigFlow table vs compact summary; Windows: key s)\n"
            "  buy <PAIR> <QUOTE_AMOUNT>\n"
            "  track <PAIR> <COIN_AMOUNT> <ENTRY_PRICE>\n"
            "  sell <PAIR> <COIN_AMOUNT>\n"
            "  close <ORDER_ID>\n"
            "  pairs list\n"
            "  pairs add <PAIR|ASSET> [MORE...]\n"
            "  pairs remove <PAIR|ASSET> [MORE...]\n"
            "  pairs reload\n\n"
            "Footer chat shortcuts:\n"
            "  Enter send command\n"
            "  Tab autocomplete\n"
            "  Up/Down recall history\n"
            "  Esc clear current input"
        )

    def execute(self, command: str, args: List[str]) -> str:
        app = self._app
        if command == "help":
            return self.format_help()

        if command == "status":
            health = app.get_health_status()
            active_pairs = ", ".join(health.get("pairs") or []) or "NONE"
            return (
                f"Status: {health.get('status')} | mode={health.get('mode')} | "
                f"simulate_only={health.get('simulate_only')} | read_only={health.get('read_only')} | "
                f"pairs={active_pairs}"
            )

        if command == "orders":
            active_orders = app.list_active_orders()
            if not active_orders:
                return "Active orders: none"
            lines = ["Active orders:"]
            for order in active_orders:
                lines.append(
                    f"  {order['order_id']} | {order['symbol']} | {order['side'].upper()} | "
                    f"remaining={order['remaining_amount']:.8f} | entry={order['entry_price']:,.4f}"
                )
            return "\n".join(lines)

        if command == "mode":
            if not args or args[0].lower() == "show":
                result = app.get_runtime_mode_status()
                enabled = ", ".join(result["enabled_strategies"]) or "NONE"
                return (
                    f"Strategy mode: {result['active_mode']} | timeframe={result['timeframe']} | "
                    f"strategies={enabled} | path={result['config_path']}"
                )
            if len(args) == 2 and args[0].lower() == "set":
                result = app.set_runtime_strategy_mode(args[1])
                return (
                    f"Strategy mode saved: {result['active_mode']} | path={result['config_path']} | "
                    "restart bot to apply fully"
                )
            if len(args) == 3 and args[0].lower() == "set" and args[2].lower() == "restart":
                result = app.set_runtime_strategy_mode(args[1])
                app.request_process_restart(reason=f"mode change to {result['active_mode']}")
                return (
                    f"Strategy mode saved: {result['active_mode']} | path={result['config_path']} | "
                    "restarting now"
                )
            return "Usage: mode show | mode set <standard|trend_only|scalping>"

        if command == "risk":
            if not args or args[0].lower() == "show":
                risk = float((app.config.get("risk", {}) or {}).get("max_risk_per_trade_pct", 0.0) or 0.0)
                level, _ = app._derive_risk_level()
                risk_cfg = app.config.get("risk", {}) or {}
                lines = [
                    f"Risk: {risk:.2f}% per trade ({level})",
                    f"SL: {risk_cfg.get('stop_loss_pct', '-')}% | TP: {risk_cfg.get('take_profit_pct', '-')}%",
                    f"Max positions: {risk_cfg.get('max_open_positions', '-')} | Max daily trades: {risk_cfg.get('max_daily_trades', '-')}",
                    f"Daily loss limit: {risk_cfg.get('max_daily_loss_pct', '-')}% | Cooldown: {risk_cfg.get('cool_down_minutes', '-')}m",
                ]
                bot_ref = app.bot
                risk_manager = getattr(bot_ref, "risk_manager", None) if bot_ref else None
                if bot_ref and risk_manager:
                    portfolio_state = bot_ref._get_portfolio_state() if hasattr(bot_ref, "_get_portfolio_state") else {}
                    portfolio_value = (
                        bot_ref._get_risk_portfolio_value(portfolio_state)
                        if hasattr(bot_ref, "_get_risk_portfolio_value")
                        else float(portfolio_state.get("total_balance", portfolio_state.get("balance", 0)) or 0)
                    )
                    rs = risk_manager.get_risk_summary(portfolio_value)
                    lines.append(
                        f"Today: {rs.get('trades_today', 0)}/{rs.get('max_daily_trades', '-')} trades | Loss: {rs.get('daily_loss', 0):.2f}/{rs.get('daily_loss_max', 0):.2f} quote ({rs.get('daily_loss_pct', 0):.2f}%)"
                    )
                    lines.append(f"Cooldown: {rs.get('cooling_down_display', 'Yes' if rs.get('cooling_down') else 'No')}")
                return "\n".join(lines)
            if len(args) == 2 and args[0].lower() == "set":
                result = app.set_runtime_risk_pct(float(args[1]))
                return f"Runtime risk updated to {result['risk_pct']:.2f}% per trade ({result['risk_level']})"
            return "Usage: risk show | risk set <percent>"

        if command in {"sigflow", "sf", "s"}:
            if args and args[0].lower() in {"full", "on", "1", "true", "yes"}:
                app._cli_sigflow_full = True
            elif args and args[0].lower() in {"compact", "off", "0", "false", "no"}:
                app._cli_sigflow_full = False
            else:
                app._cli_sigflow_full = not bool(getattr(app, "_cli_sigflow_full", False))
            return (
                "SigFlow: full pair table (all strategies)"
                if app._cli_sigflow_full
                else "SigFlow: compact summary (PASS lines + counts; type sigflow again or s to expand)"
            )

        if command == "ui":
            if not args:
                return (
                    f"UI settings: log={app._cli_log_level_filter}+ | footer={app._cli_footer_mode}. "
                    "Use: ui log <debug|info|warning|error|critical> | ui footer <compact|verbose>"
                )

            if len(args) == 2 and args[0].lower() == "log":
                selected = app._normalize_cli_log_level(args[1])
                valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
                if selected not in valid_levels:
                    return "Usage: ui log <debug|info|warning|error|critical>"
                app._cli_log_level_filter = selected
                return f"UI log filter set to {selected}+"

            if len(args) == 2 and args[0].lower() == "footer":
                mode = app._normalize_cli_footer_mode(args[1])
                if mode not in {"compact", "verbose"}:
                    return "Usage: ui footer <compact|verbose>"
                app._cli_footer_mode = mode
                return f"UI footer mode set to {mode}"

            return "Usage: ui | ui log <debug|info|warning|error|critical> | ui footer <compact|verbose>"

        if command == "buy":
            if len(args) != 2:
                return "Usage: buy <PAIR> <QUOTE_AMOUNT>"
            result = app.submit_manual_market_buy(args[0], float(args[1]))
            return (
                f"Market BUY submitted: {result['symbol']} {result['quote_amount']:.2f} quote | "
                f"order_id={result['order_id']} | filled_price={result['filled_price']:,.4f}"
            )

        if command == "track":
            if len(args) != 3:
                return "Usage: track <PAIR> <COIN_AMOUNT> <ENTRY_PRICE>"
            result = app.track_manual_position(args[0], float(args[1]), float(args[2]))
            return (
                f"Tracked position: {result['symbol']} {result['amount']:.8f} @ {result['entry_price']:,.4f} | "
                f"order_id={result['order_id']} | SL={result['stop_loss']:,.4f} | TP={result['take_profit']:,.4f}"
            )

        if command == "sell":
            if len(args) == 1:
                result = app.submit_manual_market_sell(args[0])
                return (
                    f"Market SELL submitted: {result['symbol']} {result['amount']:.8f} | "
                    f"order_id={result['order_id']}"
                )
            if len(args) == 2:
                result = app.submit_manual_market_sell(args[0], float(args[1]))
                return (
                    f"Market SELL submitted: {result['symbol']} {result['amount']:.8f} | "
                    f"order_id={result['order_id']}"
                )
            return "Usage: sell <PAIR> <COIN_AMOUNT> or sell <ORDER_ID>"

        if command == "close":
            if len(args) != 1:
                return "Usage: close <ORDER_ID>"
            result = app.submit_manual_market_sell(args[0])
            return (
                f"Active order closed via market SELL: {result['symbol']} {result['amount']:.8f} | "
                f"closed={result['closed_order_id']}"
            )

        if command == "pairs":
            if not args or args[0].lower() == "list":
                result = app.get_runtime_pairlist_status()
                configured = ", ".join(result["configured_pairs"]) or "NONE"
                active = ", ".join(result["active_pairs"]) or "NONE"
                return f"Pairlist: configured={configured} | active={active} | path={result['pairlist_path']}"
            subcommand = args[0].lower()
            if subcommand == "add" and len(args) >= 2:
                result = app.add_runtime_pairs(args[1:])
                added = ", ".join(result["added_pairs"]) or "none"
                active = ", ".join(result["active_pairs"]) or "NONE"
                return f"Pairs added: {added} | active={active}"
            if subcommand == "remove" and len(args) >= 2:
                result = app.remove_runtime_pairs(args[1:])
                removed = ", ".join(result["removed_pairs"]) or "none"
                active = ", ".join(result["active_pairs"]) or "NONE"
                return f"Pairs removed: {removed} | active={active}"
            if subcommand == "reload":
                active_pairs = app.refresh_runtime_pairs(reason="cli pair reload", force=True)
                active = ", ".join(active_pairs) or "NONE"
                return f"Runtime pairs reloaded: {active}"
            return "Usage: pairs list | pairs add <PAIR...> | pairs remove <PAIR...> | pairs reload"

        return f"Unknown command: {command}. Type 'help'"


def execute_cli_command(app: Any, command: str, args: List[str]) -> str:
    """Stateful-free entrypoint for callers that do not hold a dispatcher instance."""
    return CliCommandDispatcher(app).execute(command, args)
