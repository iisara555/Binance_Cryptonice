"""Where core risk (``RiskManager``) meets plugin protections (``plugins.protections``).

- **RiskManager** — position sizing, daily trade limits, loss rails, SL/TP maths.
- **IProtection** plugins — additive pre-trade gates (cooldown, streak, max drawdown guards).

Avoid duplicating daily PnL ceilings: keep a single source in ``RiskManager`` persistence;
protections should enforce *additional* policy, not second copies of the same counters.
"""

PLUGIN_PROTECTIONS_PKG = "plugins.protections"
