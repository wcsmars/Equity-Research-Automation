"""Default assumptions and tunable constants for the valuation engine.

These are sensible, documented defaults for a US large-cap as of ~2025. They can
be overridden per-run via the CLI or by passing custom assumption objects to the
engine. Keep all magic numbers here so the models stay clean.
"""

from __future__ import annotations

import os

# --- Macro / CAPM defaults (decimals) --------------------------------------- #
DEFAULT_RISK_FREE_RATE = 0.042       # ~10y US Treasury yield
DEFAULT_EQUITY_RISK_PREMIUM = 0.05   # Damodaran-style mature-market ERP
DEFAULT_MARGINAL_TAX_RATE = 0.21     # US federal statutory corporate rate
DEFAULT_CREDIT_SPREAD = 0.015        # added to rf when cost of debt can't be derived
MIN_EFFECTIVE_TAX_RATE = 0.0
MAX_EFFECTIVE_TAX_RATE = 0.35        # clamp derived effective tax rates to sane band
DEFAULT_BETA = 1.0                   # fallback if market beta is unavailable

# --- DCF defaults ----------------------------------------------------------- #
DEFAULT_FORECAST_YEARS = 5
DEFAULT_TERMINAL_GROWTH = 0.025      # ~ long-run nominal GDP, must be < WACC
MAX_TERMINAL_GROWTH_VS_WACC = 0.01   # require WACC - g >= this gap; else clamp g
DEFAULT_REVENUE_GROWTH_CAP = 0.30    # cap derived near-term growth at 30%/yr
DEFAULT_REVENUE_GROWTH_FLOOR = -0.05

# --- Comps defaults --------------------------------------------------------- #
DEFAULT_PEER_LIMIT = 8               # max peers pulled when auto-suggesting
COMPS_MULTIPLES = ("ev_ebitda", "ev_sales", "pe", "pb", "peg")
# Outlier trimming: drop peers whose multiple is outside [median/k, median*k]
COMPS_OUTLIER_FACTOR = 3.0

# --- Sensitivity grid defaults ---------------------------------------------- #
SENSITIVITY_WACC_DELTAS = (-0.015, -0.0075, 0.0, 0.0075, 0.015)   # absolute +/- on WACC
SENSITIVITY_GROWTH_DELTAS = (-0.01, -0.005, 0.0, 0.005, 0.01)     # absolute +/- on terminal g
SENSITIVITY_MARGIN_DELTAS = (-0.02, -0.01, 0.0, 0.01, 0.02)       # absolute +/- on EBIT margin
SENSITIVITY_EXIT_MULTIPLE_DELTAS = (-2.0, -1.0, 0.0, 1.0, 2.0)    # absolute +/- on EV/EBITDA

# --- HTTP / EDGAR ----------------------------------------------------------- #
# SEC requires a descriptive User-Agent with contact info on every request.
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "").strip()
SEC_REQUEST_TIMEOUT = 20             # seconds
SEC_MAX_RETRIES = 3
HTTP_RETRY_BACKOFF = 1.5             # seconds, exponential

# --- Output ----------------------------------------------------------------- #
DEFAULT_OUTPUT_DIR = "output"
CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}
