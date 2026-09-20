"""Automated equity-valuation engine.

Pulls fundamentals (SEC EDGAR) and market data (yfinance), then produces a DCF,
trading comps, DDM/FCFE, sensitivity tables and a football-field summary, and
exports an Excel model plus an interactive HTML report.

Public entry point:
    from equity_valuation import value_company
    report = value_company("AAPL")
"""

from __future__ import annotations

__version__ = "0.1.0"

# The engine import is the public surface; submodules are imported lazily inside
# engine.value_company to keep `import equity_valuation` cheap and avoid hard
# failures if an optional dependency (e.g. yfinance) is missing at import time.
from .engine import value_company  # noqa: E402,F401

__all__ = ["value_company", "__version__"]
