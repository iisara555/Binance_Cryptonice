"""
Unified alerting / notification entrypoints (Telegram, rate limits, optional metrics).

Import from here in new code instead of reaching into ``alerts`` / ``monitoring`` ad hoc.
"""

from alerts import AlertLevel, AlertSystem

__all__ = ["AlertLevel", "AlertSystem"]
