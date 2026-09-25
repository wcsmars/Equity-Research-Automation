"""Orchestration: pull data, run every model, assemble a ValuationReport.

This is the public entry point. Each model is run defensively so that one model
failing (e.g. no dividends -> no DDM, or a thin EDGAR record) never sinks the
whole valuation -- failures become `warnings` on the report, as do the fallbacks
and clamps each model records in its notes (prefixed with the model name) and
any method left out of the blended target.
"""

from __future__ import annotations

from typing import Optional

from . import config
from .schemas import (
    DCFAssumptions,
    DDMAssumptions,
    FootballFieldRow,
    MacroAssumptions,
    ValuationReport,
)
from .utils import is_num, median


def value_company(
    ticker: str,
    *,
    provider=None,
    macro: Optional[MacroAssumptions] = None,
    dcf_assumptions: Optional[DCFAssumptions] = None,
    ddm_assumptions: Optional[DDMAssumptions] = None,
    peers: Optional[list[str]] = None,
    run_dcf: bool = True,
    run_comps: bool = True,
    run_ddm: bool = True,
    run_fcfe: bool = True,
    run_sensitivity: bool = True,
) -> ValuationReport:
    """Value `ticker` and return a fully-assembled ValuationReport.

    Parameters mirror the CLI flags. `provider` defaults to the EDGAR+yfinance
    HybridProvider. Any model can be toggled off.
    """
    ticker = ticker.strip().upper()
    macro = macro or MacroAssumptions()
    dcf_assumptions = dcf_assumptions or DCFAssumptions()
    ddm_assumptions = ddm_assumptions or DDMAssumptions()

    # Lazy imports so a missing optional dep surfaces only when actually used.
    if provider is None:
        from .data.provider import HybridProvider

        provider = HybridProvider()

    company = provider.get_company_data(ticker)
    current_price = company.market.price

    report = ValuationReport(
        company=company,
        macro=macro,
        current_price=current_price,
    )
    report.warnings.extend(company.source_notes)

    # --- DCF ---------------------------------------------------------------- #
    if run_dcf:
        try:
            from .models.dcf import run_dcf as _run_dcf

            report.dcf = _run_dcf(company, macro, dcf_assumptions, current_price)
            _add_notes(report, "WACC", report.dcf.wacc.detail.get("notes"))
            _add_notes(report, "DCF", report.dcf.assumptions.get("notes"))
        except Exception as exc:  # noqa: BLE001 - degrade, don't crash
            report.warnings.append(f"DCF failed: {exc}")

    # --- Trading comps ------------------------------------------------------ #
    if run_comps:
        try:
            from .models.comps import run_comps as _run_comps

            report.comps = _run_comps(company, provider, peers, current_price)
            if report.comps is not None and not report.comps.peers:
                report.warnings.append(
                    "Comps: no usable peers found (pass --peers to supply them)."
                )
        except Exception as exc:  # noqa: BLE001
            report.warnings.append(f"Comps failed: {exc}")

    # --- DDM ---------------------------------------------------------------- #
    if run_ddm:
        try:
            from .models.ddm_fcfe import run_ddm as _run_ddm

            report.ddm = _run_ddm(company, macro, ddm_assumptions, current_price)
            if report.ddm is None:
                report.warnings.append("DDM skipped: company pays no dividend.")
            else:
                _add_notes(report, "DDM", report.ddm.detail.get("notes"))
        except Exception as exc:  # noqa: BLE001
            report.warnings.append(f"DDM failed: {exc}")

    # --- FCFE --------------------------------------------------------------- #
    if run_fcfe:
        try:
            from .models.ddm_fcfe import run_fcfe as _run_fcfe

            report.fcfe = _run_fcfe(company, macro, ddm_assumptions, current_price)
            _add_notes(report, "FCFE", report.fcfe.detail.get("notes"))
        except Exception as exc:  # noqa: BLE001
            report.warnings.append(f"FCFE failed: {exc}")

    # --- Sensitivity (depends on DCF being viable) -------------------------- #
    if run_sensitivity and report.dcf is not None:
        try:
            from .models.sensitivity import dcf_sensitivity

            report.sensitivities = dcf_sensitivity(
                company, macro, dcf_assumptions, current_price
            )
        except Exception as exc:  # noqa: BLE001
            report.warnings.append(f"Sensitivity failed: {exc}")

    # --- Football field + blended target ------------------------------------ #
    try:
        from .models.sensitivity import build_football_field

        report.football_field = build_football_field(report)
    except Exception as exc:  # noqa: BLE001
        report.warnings.append(f"Football field failed: {exc}")
        report.football_field = _fallback_football_field(report)

    report.summary = _build_summary(report)
    return report


def _add_notes(report: ValuationReport, label: str, notes) -> None:
    """Surface a model's recorded fallbacks/clamps as ``"<label>: <note>"`` warnings."""
    for note in notes or []:
        warning = f"{label}: {note}"
        if warning not in report.warnings:
            report.warnings.append(warning)


def _fallback_football_field(report: ValuationReport) -> list[FootballFieldRow]:
    """Minimal football field if the model helper failed -- one bar per method.

    Self-contained on purpose (it runs when models.sensitivity failed). Bands are
    ordered so low <= base <= high holds for negative prices too.
    """
    def band(name: str, p, pct: float) -> Optional[FootballFieldRow]:
        if not is_num(p):
            return None
        a, b = p * (1.0 - pct), p * (1.0 + pct)
        return FootballFieldRow(name, min(a, b), p, max(a, b))

    rows: list[FootballFieldRow] = []
    m = report.company.market
    if m.fifty_two_week_low and m.fifty_two_week_high:
        rows.append(
            FootballFieldRow(
                "52-week range", m.fifty_two_week_low, report.current_price, m.fifty_two_week_high
            )
        )
    if report.dcf:
        rows.append(band("DCF", report.dcf.implied_price, 0.15))
    if report.comps and report.comps.implied_price_summary:
        s = report.comps.implied_price_summary
        if s.get("low") and s.get("high"):
            rows.append(
                FootballFieldRow("Comps", s["low"], s.get("median", report.current_price), s["high"])
            )
    if report.ddm:
        rows.append(band("DDM", report.ddm.implied_price, 0.10))
    if report.fcfe:
        rows.append(band("FCFE", report.fcfe.implied_price, 0.10))
    return [r for r in rows if r is not None]


def _build_summary(report: ValuationReport) -> dict:
    """Collect each method's central estimate and a blended (median) target.

    ``methods`` keeps every method's value for display. The blend differs in two
    ways, each recorded in ``report.warnings``: a price of exactly 0 (the
    placeholder the models return when they cannot value the company) or a
    non-finite price is left out, and a negative price (claims senior to common
    equity exceed the value) counts as 0, because equity cannot be worth less.
    """
    methods: dict[str, float] = {}
    if report.dcf:
        methods["DCF"] = report.dcf.implied_price
    if report.comps and report.comps.implied_price_summary.get("median"):
        methods["Comps (median)"] = report.comps.implied_price_summary["median"]
    if report.ddm:
        methods["DDM"] = report.ddm.implied_price
    if report.fcfe:
        methods["FCFE"] = report.fcfe.implied_price

    blend: list[float] = []
    for name, price in methods.items():
        if is_num(price) and price > 0:
            blend.append(price)
            continue
        if is_num(price) and price < 0:
            blend.append(0.0)
            warning = (f"{name} implies negative equity ({price:.2f} per share); "
                       "counted as 0.00 in the blended target.")
        else:
            reason = "non-finite implied price" if not is_num(price) else "no valuation (0.00)"
            warning = f"{name} excluded from blended target: {reason}."
        if warning not in report.warnings:
            report.warnings.append(warning)

    blended = median(blend)
    cur = report.current_price
    return {
        "ticker": report.company.ticker,
        "name": report.company.name,
        "currency": report.company.market.currency,
        "current_price": cur,
        "methods": methods,
        "blended_target": blended,
        "blended_upside": (blended / cur - 1.0) if (blended and cur) else None,
        "recommendation": _recommendation(blended, cur),
    }


def _recommendation(target: Optional[float], price: Optional[float]) -> str:
    if not target or not price:
        return "N/A"
    upside = target / price - 1.0
    if upside >= 0.15:
        return "Undervalued"
    if upside <= -0.15:
        return "Overvalued"
    return "Fairly valued"
