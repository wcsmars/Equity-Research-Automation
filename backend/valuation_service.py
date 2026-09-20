"""Drive the valuation engine from API requests.

Responsibilities:
  * translate the frontend's flat assumption payload into the engine's
    MacroAssumptions / DCFAssumptions / DDMAssumptions objects;
  * cache the expensive CompanyData fetch per ticker so moving an assumption
    slider re-runs the models instantly instead of re-hitting EDGAR/yfinance;
  * return the serialized report plus an echo of the assumptions actually used.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

from equity_valuation import value_company
from equity_valuation.data.base import DataProvider
from equity_valuation.schemas import (
    DCFAssumptions,
    DDMAssumptions,
    MacroAssumptions,
)

from .serialization import report_to_dict

# Engine config defaults (mirrored here so the API can echo resolved values).
from equity_valuation import config as _cfg

_CACHE_TTL = 600.0  # seconds to reuse fetched CompanyData for slider re-runs
_cache_lock = threading.Lock()
_company_cache: dict[str, tuple[float, Any, DataProvider]] = {}


_peer_cache_lock = threading.Lock()
_peer_cache: dict[tuple, tuple[float, list]] = {}  # tickers tuple -> (ts, rows)
_PEER_TTL = 600.0


class _CachedProvider(DataProvider):
    """Serves a pre-fetched CompanyData; delegates live calls to the underlying
    provider, with a short-lived cache for peer comps so assumption-slider
    recomputes don't re-fetch every peer from yfinance each time."""

    def __init__(self, base: DataProvider, company_data):
        self._base = base
        self._cd = company_data

    def get_company_data(self, ticker: str):
        return self._cd

    def get_market_data(self, ticker: str):
        return self._cd.market

    def get_peer_comp_rows(self, tickers):
        key = tuple(sorted(t.upper() for t in tickers))
        now = time.time()
        with _peer_cache_lock:
            hit = _peer_cache.get(key)
            if hit and now - hit[0] < _PEER_TTL:
                return hit[1]
        rows = self._base.get_peer_comp_rows(tickers)
        if rows:  # don't cache an empty/failed fetch
            with _peer_cache_lock:
                _peer_cache[key] = (now, rows)
        return rows

    def suggest_peers(self, ticker: str):
        return self._base.suggest_peers(ticker)


def _get_provider(ticker: str, refresh: bool = False) -> DataProvider:
    """Return a provider that yields cached CompanyData when fresh."""
    ticker = ticker.strip().upper()
    now = time.time()
    with _cache_lock:
        hit = _company_cache.get(ticker)
        if hit and not refresh and (now - hit[0]) < _CACHE_TTL:
            return _CachedProvider(hit[2], hit[1])

    # Cold path: fetch once, cache, then serve from cache for this run too.
    from equity_valuation.data.provider import HybridProvider

    base = HybridProvider()
    company_data = base.get_company_data(ticker)  # the slow network call
    with _cache_lock:
        _company_cache[ticker] = (now, company_data, base)
    return _CachedProvider(base, company_data)


def _f(payload: dict, *keys) -> Optional[float]:
    for k in keys:
        if payload.get(k) is not None:
            try:
                return float(payload[k])
            except (TypeError, ValueError):
                return None
    return None


def _revenue_growth_path(
    y1: Optional[float], terminal_growth: float, forecast_years: int
) -> Optional[list[float]]:
    """Build an explicit fading growth path from a near-term (year-1) override
    down to terminal growth, so the AI's 'raise near-term growth' suggestion is
    directly applicable. Falls back to None (engine derives its own) if no y1."""
    if y1 is None or forecast_years < 1:
        return None
    try:
        from equity_valuation.utils import fade_path

        path = list(fade_path(y1, terminal_growth, forecast_years))
        if len(path) == forecast_years:
            return path
    except Exception:  # noqa: BLE001 - fall through to manual fade
        pass
    if forecast_years == 1:
        return [y1]
    step = (terminal_growth - y1) / (forecast_years - 1)
    return [y1 + step * i for i in range(forecast_years)]


def parse_assumptions(payload: dict):
    """payload (all optional) -> (macro, dcf, ddm, peers, toggles, echo)."""
    payload = payload or {}

    rf = _f(payload, "rf", "risk_free_rate")
    erp = _f(payload, "erp", "equity_risk_premium")
    tax = _f(payload, "tax_rate", "tax")
    cod = _f(payload, "cost_of_debt", "pretax_cost_of_debt")
    try:
        forecast_years = int(float(payload.get("forecast_years")))
    except (TypeError, ValueError):
        forecast_years = _cfg.DEFAULT_FORECAST_YEARS
    forecast_years = max(1, min(forecast_years, 15))
    terminal_growth = _f(payload, "terminal_growth")
    if terminal_growth is None:
        terminal_growth = _cfg.DEFAULT_TERMINAL_GROWTH
    terminal_method = payload.get("terminal_method")
    if terminal_method not in ("gordon", "exit_multiple"):
        terminal_method = "gordon"
    exit_ev_ebitda = _f(payload, "exit_ev_ebitda")
    target_ebit_margin = _f(payload, "target_ebit_margin")
    rev_y1 = _f(payload, "revenue_growth_y1")
    rev_path = payload.get("revenue_growth")
    if not isinstance(rev_path, list) or not rev_path:
        rev_path = _revenue_growth_path(rev_y1, terminal_growth, forecast_years)

    macro = MacroAssumptions(
        risk_free_rate=rf if rf is not None else _cfg.DEFAULT_RISK_FREE_RATE,
        equity_risk_premium=erp
        if erp is not None
        else _cfg.DEFAULT_EQUITY_RISK_PREMIUM,
        tax_rate=tax,
        pretax_cost_of_debt=cod,
    )
    dcf = DCFAssumptions(
        forecast_years=forecast_years,
        revenue_growth=rev_path,
        terminal_growth=terminal_growth,
        terminal_method=terminal_method,
        exit_ev_ebitda=exit_ev_ebitda,
        target_ebit_margin=target_ebit_margin,
        tax_rate=tax,
    )
    ddm = DDMAssumptions(
        forecast_years=forecast_years, terminal_growth=terminal_growth
    )

    peers_raw = payload.get("peers")
    if isinstance(peers_raw, str):
        peers = [p.strip().upper() for p in peers_raw.split(",") if p.strip()]
    elif isinstance(peers_raw, list):
        peers = [str(p).strip().upper() for p in peers_raw if str(p).strip()]
    else:
        peers = None

    toggles = {
        "run_dcf": payload.get("run_dcf", True),
        "run_comps": payload.get("run_comps", True),
        "run_ddm": payload.get("run_ddm", True),
        "run_fcfe": payload.get("run_fcfe", True),
        "run_sensitivity": payload.get("run_sensitivity", True),
    }

    echo = {
        "rf": macro.risk_free_rate,
        "erp": macro.equity_risk_premium,
        "tax_rate": tax,
        "cost_of_debt": cod,
        "forecast_years": forecast_years,
        "terminal_growth": terminal_growth,
        "terminal_method": terminal_method,
        "exit_ev_ebitda": exit_ev_ebitda,
        "target_ebit_margin": target_ebit_margin,
        "revenue_growth_y1": rev_y1,
        "revenue_growth": rev_path,
        # Echo as CSV — the frontend's Assumptions.peers is a string.
        "peers": ",".join(peers) if peers else None,
    }
    return macro, dcf, ddm, peers, toggles, echo


def run_valuation_report(ticker: str, payload: Optional[dict] = None):
    """Provider (cached) -> value_company. Returns (ValuationReport, echo).

    Used directly by the export endpoints, which need the engine's dataclass
    (write_excel / write_html bind to it) rather than the serialized dict."""
    payload = payload or {}
    macro, dcf, ddm, peers, toggles, echo = parse_assumptions(payload)
    provider = _get_provider(ticker, refresh=bool(payload.get("refresh")))

    report = value_company(
        ticker,
        provider=provider,
        macro=macro,
        dcf_assumptions=dcf,
        ddm_assumptions=ddm,
        peers=peers,
        **toggles,
    )
    return report, echo


def _reverse_dcf(report, dcf_assumptions, macro) -> Optional[dict]:
    """Solve for the year-1 revenue growth the market price implies, holding
    every other assumption fixed (growth fades to terminal as usual).

    This is the 'what do I have to believe?' number: if the market-implied
    growth looks heroic vs history, the price embeds optimism — and vice versa.
    Pure-math re-runs of the engine's run_dcf on cached data (fast)."""
    import dataclasses as _dc

    if report.dcf is None:
        return None
    try:
        from equity_valuation.models.dcf import run_dcf as _run_dcf

        company = report.company
        price = report.current_price
        if not price or price <= 0:
            return None

        def implied(g1: float) -> Optional[float]:
            path = _revenue_growth_path(
                g1, dcf_assumptions.terminal_growth, dcf_assumptions.forecast_years
            )
            a = _dc.replace(dcf_assumptions, revenue_growth=path)
            try:
                return _run_dcf(company, macro, a, price).implied_price
            except Exception:  # noqa: BLE001
                return None

        lo, hi = -0.40, 0.80
        p_lo, p_hi = implied(lo), implied(hi)
        base = {
            "current_assumption_y1": (dcf_assumptions.revenue_growth or [None])[0]
            if dcf_assumptions.revenue_growth
            else None,
        }
        if p_lo is None or p_hi is None:
            return None
        # implied price is monotonically increasing in growth
        if (p_lo - price) * (p_hi - price) > 0:
            return {
                **base,
                "converged": False,
                "implied_growth_y1": None,
                "note": "Market price is outside the solvable growth range "
                f"({lo:.0%} to {hi:.0%}) with the current assumptions.",
            }
        mid = (lo + hi) / 2.0
        for _ in range(48):
            mid = (lo + hi) / 2.0
            p_mid = implied(mid)
            if p_mid is None:
                return None
            if abs(p_mid - price) < 0.005:
                break
            if (p_mid - price) * (p_lo - price) > 0:
                lo, p_lo = mid, p_mid
            else:
                hi = mid
        return {**base, "converged": True, "implied_growth_y1": mid}
    except Exception:  # noqa: BLE001 - diagnostics only, never block valuation
        return None


def run_valuation(ticker: str, payload: Optional[dict] = None) -> dict:
    """Full pipeline: provider (cached) -> value_company -> serialized dict,
    plus the reverse-DCF (market-implied growth) diagnostic."""
    payload = payload or {}
    report, echo = run_valuation_report(ticker, payload)
    d = report_to_dict(report, assumptions_used=echo)

    macro, dcf, _ddm, _peers, _toggles, _echo = parse_assumptions(payload)
    from .serialization import _sanitize  # reverse_dcf rides after report_to_dict

    d["reverse_dcf"] = _sanitize(_reverse_dcf(report, dcf, macro))
    return d
