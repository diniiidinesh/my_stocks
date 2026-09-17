"""Offline alert research / backtests (run on the Kite-whitelisted VM)."""

from nse_alert.research.analyze import analyze_local_events, format_analysis
from nse_alert.research.runner import run_backtest, format_backtest_report

__all__ = [
    "analyze_local_events",
    "format_analysis",
    "run_backtest",
    "format_backtest_report",
]
