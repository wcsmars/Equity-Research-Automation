"""Turn the engine's `ValuationReport` dataclass graph into a JSON-safe dict
(the contract the frontend binds to), and build a compact text context that
grounds the AI researcher in the currently-loaded model.

The JSON shape mirrors `equity_valuation/schemas.py` field-for-field, with two
additions the engine doesn't emit directly:
  * `company.balance_sheet.net_debt` (a dataclass @property asdict drops)
  * `assumptions_used` (echo of the knobs that produced this run)
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any, Optional


def _sanitize(obj: Any) -> Any:
    """Recursively replace NaN/Inf floats with None so the payload is valid JSON."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def report_to_dict(report, assumptions_used: Optional[dict] = None) -> dict:
    """Serialize a ValuationReport to a JSON-safe dict."""
    d = dataclasses.asdict(report)

    # asdict() drops @property values — re-attach net_debt where present.
    try:
        d["company"]["balance_sheet"]["net_debt"] = report.company.balance_sheet.net_debt
    except Exception:  # noqa: BLE001 - degrade, never block serialization
        pass

    if assumptions_used is not None:
        d["assumptions_used"] = assumptions_used

    return _sanitize(d)


# --------------------------------------------------------------------------- #
#  AI grounding context
# --------------------------------------------------------------------------- #
def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _money(x: Optional[float], sym: str = "") -> str:
    return "n/a" if x is None else f"{sym}{x:,.2f}"


def _big(x: Optional[float], sym: str = "") -> str:
    """Compact large-number formatter (e.g. 391.0B)."""
    if x is None:
        return "n/a"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if abs(x) >= div:
            return f"{sym}{x / div:,.1f}{unit}"
    return f"{sym}{x:,.0f}"


def build_ai_context(report: dict) -> str:
    """A compact (~1k token) snapshot of the loaded valuation, fed to Claude so
    its analysis and assumption suggestions reference the actual current model."""
    s = report.get("summary", {}) or {}
    company = report.get("company", {}) or {}
    market = company.get("market", {}) or {}
    fin = company.get("financials", {}) or {}
    bs = company.get("balance_sheet", {}) or {}
    macro = report.get("macro", {}) or {}
    dcf = report.get("dcf") or {}
    comps = report.get("comps") or {}
    sym = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}.get(
        s.get("currency"), ""
    )

    lines: list[str] = []
    lines.append(
        f"COMPANY: {s.get('name','?')} ({s.get('ticker','?')}) | "
        f"sector={market.get('sector')} industry={market.get('industry')}"
    )
    lines.append(
        f"PRICE: {_money(s.get('current_price'), sym)} {s.get('currency','')} | "
        f"market cap {_big(market.get('market_cap'), sym)} | "
        f"beta {market.get('beta')} | "
        f"52w {_money(market.get('fifty_two_week_low'), sym)}-{_money(market.get('fifty_two_week_high'), sym)}"
    )
    lines.append(
        f"VERDICT: {s.get('recommendation','?')} | "
        f"blended target {_money(s.get('blended_target'), sym)} "
        f"({_pct(s.get('blended_upside'))} vs price)"
    )
    methods = s.get("methods") or {}
    if methods:
        lines.append(
            "METHOD VALUES: "
            + "; ".join(f"{k} {_money(v, sym)}" for k, v in methods.items())
        )

    # Latest fundamentals — every value may be None (sanitized NaN), so all
    # arithmetic must be None-safe or one missing EBIT silently destroys the
    # AI's entire model context (the caller swallows exceptions).
    def last(key):
        seq = fin.get(key) or []
        return seq[-1] if seq else None

    def _ratio(num, den):
        if num is None or den is None or not den:
            return None
        try:
            return num / den
        except TypeError:
            return None

    years = fin.get("fiscal_years") or []
    if years:
        rev = fin.get("revenue") or []
        lines.append(
            f"LATEST FY{years[-1]}: revenue {_big(last('revenue'), sym)}, "
            f"EBIT {_big(last('ebit'), sym)}, net income {_big(last('net_income'), sym)}, "
            f"EBIT margin {_pct(_ratio(last('ebit'), last('revenue')))}"
        )
        first, latest = (rev[0] if rev else None), (rev[-1] if rev else None)
        n = len(rev) - 1
        if (
            n >= 1
            and isinstance(first, (int, float))
            and isinstance(latest, (int, float))
            and first > 0
            and latest > 0
        ):
            cagr = (latest / first) ** (1 / n) - 1
            lines.append(f"HISTORICAL REVENUE CAGR ({n}y): {_pct(cagr)}")
    lines.append(
        f"BALANCE SHEET: total debt {_big(bs.get('total_debt'), sym)}, "
        f"cash {_big(bs.get('cash_and_investments'), sym)}, "
        f"net debt {_big(bs.get('net_debt'), sym)}"
    )

    # DCF drivers (the editable assumptions the user is steering)
    if dcf:
        wacc = (dcf.get("wacc") or {})
        da = dcf.get("assumptions") or {}
        lines.append(
            f"DCF: WACC {_pct(wacc.get('wacc'))} (ke {_pct(wacc.get('cost_of_equity'))}, "
            f"beta {wacc.get('beta')}), terminal growth {_pct(da.get('terminal_growth'))}, "
            f"terminal method {da.get('terminal_method')}, "
            f"forecast years {da.get('forecast_years')}, "
            f"implied {_money(dcf.get('implied_price'), sym)} ({_pct(dcf.get('upside'))})"
        )
        rg = da.get("revenue_growth")
        if isinstance(rg, list) and rg:
            lines.append(
                "DCF revenue-growth path: " + ", ".join(_pct(g) for g in rg)
            )
    lines.append(
        f"MACRO: risk-free {_pct(macro.get('risk_free_rate'))}, "
        f"ERP {_pct(macro.get('equity_risk_premium'))}, "
        f"tax {_pct(macro.get('tax_rate'))}"
    )

    # Comps snapshot
    if comps:
        target = comps.get("target") or {}
        stats = comps.get("stats") or {}

        def med(m):
            return (stats.get(m) or {}).get("median")

        lines.append(
            "TARGET MULTIPLES: "
            f"P/E {target.get('pe')}, EV/EBITDA {target.get('ev_ebitda')}, "
            f"EV/Sales {target.get('ev_sales')}, P/B {target.get('pb')}"
        )
        lines.append(
            "PEER MEDIANS: "
            f"P/E {med('pe')}, EV/EBITDA {med('ev_ebitda')}, "
            f"EV/Sales {med('ev_sales')}, P/B {med('pb')} "
            f"({len(comps.get('peers') or [])} peers)"
        )

    warnings = report.get("warnings") or []
    if warnings:
        lines.append("MODEL NOTES: " + " | ".join(warnings[:6]))

    return "\n".join(lines)
