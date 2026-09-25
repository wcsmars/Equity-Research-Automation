"""Unlevered discounted-cash-flow (FCFF) valuation.

Projects free cash flow to the firm:

    FCFF_t = NOPAT_t + D&A_t - Capex_t - dNWC_t,   NOPAT_t = EBIT_t * (1 - tax)

Cash flows are projected from the company's latest fundamentals using either
explicit per-year drivers (from ``DCFAssumptions``) or history-derived defaults,
discounted at WACC (optionally on a mid-year convention), and a terminal value
(Gordon growth or an exit EV/EBITDA multiple) is added to obtain enterprise
value, then equity value, then an implied per-share price.

Pure-Python: stdlib + the package's own helpers only. All money is absolute
units; all rates are decimals; annual series run oldest -> newest.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from ..schemas import (
    CompanyData,
    DCFAssumptions,
    DCFResult,
    MacroAssumptions,
)
from ..utils import (
    fade_path,
    incremental_ratio,
    is_num,
    mean,
    net_debt_parts,
    safe_div,
    series_cagr,
)
from .. import config
from .wacc import compute_wacc, effective_tax_rate

TERMINAL_METHODS = ("gordon", "exit_multiple")


# --------------------------------------------------------------------------- #
#  Small internal helpers
# --------------------------------------------------------------------------- #
def _latest(series, default=None):
    """Last finite element of an oldest->newest series, else ``default``."""
    if not series:
        return default
    val = series[-1]
    return val if is_num(val) else default


def _hist_ratio_mean(numerators, denominators):
    """Mean of numerator_i / denominator_i over years where both are finite and
    the denominator is positive. Returns None if no usable pair exists."""
    ratios = []
    for num, den in zip(numerators or [], denominators or []):
        if is_num(num) and is_num(den) and den > 0:
            r = safe_div(num, den)
            if r is not None:
                ratios.append(r)
    return mean(ratios)


def _all_zero(series) -> bool:
    """True if a series has entries and every finite one is 0 (a zero-filled gap)."""
    vals = [v for v in (series or []) if is_num(v)]
    return bool(vals) and all(v == 0 for v in vals)


def _ebit_history(fin) -> tuple[list, Optional[str]]:
    """Historical EBIT with zero-filled (unreported) years rebuilt from pretax.

    Providers write 0.0 when a filer has no operating-income tag (e.g. single-step
    income statements). A 0 there is a gap, not a reading, so where pretax income
    exists the year is approximated as pretax income + interest expense.
    """
    ebit = list(getattr(fin, "ebit", None) or []) if fin is not None else []
    pretax = list(getattr(fin, "pretax_income", None) or []) if fin is not None else []
    interest = list(getattr(fin, "interest_expense", None) or []) if fin is not None else []
    out, rebuilt = [], False
    for i, e in enumerate(ebit):
        p = pretax[i] if i < len(pretax) else None
        if (not is_num(e) or e == 0) and is_num(p) and p != 0:
            it = interest[i] if i < len(interest) and is_num(interest[i]) else 0.0
            out.append(p + abs(it))
            rebuilt = True
        else:
            out.append(e)
    note = ("EBIT not reported for some years; approximated as pretax income + "
            "interest expense") if rebuilt else None
    return out, note


def start_ebit_margin(fin, hist_revenue, base_revenue) -> tuple[Optional[float], Optional[str]]:
    """(starting EBIT margin, note): latest EBIT / base revenue, else the trailing
    mean margin, ignoring zero-filled EBIT years. None if nothing is usable.

    Shared with the sensitivity grid so its margin axis centres on the same start.
    """
    hist_ebit, note = _ebit_history(fin)
    reported = [e if (is_num(e) and e != 0) else None for e in hist_ebit]
    latest = reported[-1] if reported else None
    margin = safe_div(latest, base_revenue) if (is_num(base_revenue) and base_revenue > 0) else None
    if margin is None:
        # Fall back to the trailing average EBIT margin.
        margin = _hist_ratio_mean(reported, hist_revenue)
    return margin, note


def resolve_terminal_method(assumptions) -> tuple[str, Optional[str]]:
    """(terminal method actually used, note): normalise case/whitespace and fall
    back to Gordon for an unknown method or an exit multiple without a multiple."""
    raw = assumptions.terminal_method if assumptions else None
    method = str(raw or "gordon").strip().lower()
    if method not in TERMINAL_METHODS:
        return "gordon", f"unknown terminal_method {raw!r}; falling back to Gordon"
    if method == "exit_multiple" and not is_num(getattr(assumptions, "exit_ev_ebitda", None)):
        return "gordon", "exit_ev_ebitda missing for exit_multiple method; falling back to Gordon"
    return method, None


# --------------------------------------------------------------------------- #
#  Main entry point
# --------------------------------------------------------------------------- #
def run_dcf(
    company: CompanyData,
    macro: MacroAssumptions,
    assumptions: DCFAssumptions,
    current_price: float,
) -> DCFResult:
    """Run the FCFF DCF and return a fully-populated ``DCFResult``.

    Degrades gracefully on missing data: any unavailable driver falls back to a
    documented default and the choice is recorded in ``DCFResult.assumptions``
    (which carries a human-readable ``notes`` list). Raises ``ValueError`` only
    when there is no positive base revenue to project from (an FCFF DCF is not
    meaningful then; the engine records the failure as a warning).
    """
    notes: list[str] = []

    # ----- 1) discount rate ------------------------------------------------ #
    wacc_result = compute_wacc(company, macro)
    w = wacc_result.wacc
    # Guard a degenerate / non-positive WACC so discounting stays well-defined.
    if not is_num(w) or w <= 0:
        computed = w
        rf = wacc_result.detail.get("risk_free_rate")
        erp = wacc_result.detail.get("equity_risk_premium")
        # Beta-1 cost of equity on the caller's macro inputs, else config defaults.
        w = rf + erp if (is_num(rf) and is_num(erp) and rf + erp > 0) else (
            config.DEFAULT_RISK_FREE_RATE + config.DEFAULT_EQUITY_RISK_PREMIUM)
        notes.append(f"computed WACC {computed:.4f} non-positive/invalid; "
                     f"discounting at fallback rf+ERP {w:.4f}")
        # Report the rate actually used (UI, exports and sensitivity labels read
        # DCFResult.wacc.wacc); keep the rejected value for reference.
        wacc_result = replace(wacc_result, wacc=w,
                              detail={**wacc_result.detail, "wacc": w, "wacc_computed": computed})

    fin = getattr(company, "financials", None)
    bs = getattr(company, "balance_sheet", None)
    market = getattr(company, "market", None)

    n = assumptions.forecast_years if (assumptions and is_num(assumptions.forecast_years)
                                       and assumptions.forecast_years > 0) \
        else config.DEFAULT_FORECAST_YEARS
    n = int(n)

    terminal_growth = assumptions.terminal_growth if (assumptions and is_num(assumptions.terminal_growth)) \
        else config.DEFAULT_TERMINAL_GROWTH

    # ----- 2) revenue growth path ----------------------------------------- #
    hist_revenue = list(getattr(fin, "revenue", None) or []) if fin is not None else []
    base_revenue = _latest(hist_revenue)
    if not is_num(base_revenue) or base_revenue <= 0:
        # No anchor for projections: EV would be 0 and the "price" just
        # -net_debt/shares, which is not a valuation. Fail loudly instead.
        raise ValueError("no positive latest revenue to project from; an FCFF DCF is not meaningful")

    growth_path = None
    if assumptions and assumptions.revenue_growth:
        gp = [g for g in assumptions.revenue_growth if is_num(g)]
        if len(gp) >= n:
            growth_path = gp[:n]
        elif gp:
            # Pad a short explicit path by holding the last given growth flat.
            growth_path = gp + [gp[-1]] * (n - len(gp))
            notes.append("revenue_growth shorter than forecast_years; padded with last value")
    if growth_path is None:
        # Derive base growth from historical revenue CAGR over the available span
        # (zero-filled years are skipped as endpoints but still count as periods).
        base_growth = series_cagr(hist_revenue, getattr(fin, "fiscal_years", None))
        if base_growth is None:
            base_growth = terminal_growth
            notes.append("historical revenue CAGR unavailable; starting growth at terminal_growth")
        # Clamp the near-term growth into a sane band before fading.
        base_growth = max(config.DEFAULT_REVENUE_GROWTH_FLOOR,
                          min(config.DEFAULT_REVENUE_GROWTH_CAP, base_growth))
        growth_path = fade_path(base_growth, terminal_growth, n)

    # Project the revenue series (length n).
    revenue: list[float] = []
    prev = base_revenue
    for g in growth_path:
        cur = prev * (1.0 + g)
        revenue.append(cur)
        prev = cur

    # ----- 3) EBIT margin path -------------------------------------------- #
    start_margin, ebit_note = start_ebit_margin(fin, hist_revenue, base_revenue)
    if ebit_note:
        notes.append(ebit_note)
    if start_margin is None:
        start_margin = 0.0
        notes.append("EBIT margin unavailable; defaulting to 0")

    target_margin = assumptions.target_ebit_margin if (assumptions and is_num(assumptions.target_ebit_margin)) \
        else start_margin
    margin_path = fade_path(start_margin, target_margin, n)
    ebit = [rev * m for rev, m in zip(revenue, margin_path)]

    # ----- 4) tax & NOPAT -------------------------------------------------- #
    if assumptions and is_num(assumptions.tax_rate):
        tax = assumptions.tax_rate
        tax_source = "assumptions.tax_rate"
    elif macro is not None and is_num(macro.tax_rate):
        tax = macro.tax_rate
        tax_source = "macro.tax_rate"
    else:
        tax = effective_tax_rate(fin, config.DEFAULT_MARGINAL_TAX_RATE)
        tax_source = "effective (historical)"
        if tax <= 0:
            # Kept as documented (pass-throughs genuinely pay ~0%), but flag it:
            # providers also zero-fill an unreported tax line.
            notes.append("historical effective tax rate is 0% (tax expense zero or unreported); "
                         "NOPAT is untaxed -- set a tax rate if that is not intended")
    nopat = [e * (1.0 - tax) for e in ebit]

    # ----- 5) D&A, Capex, dNWC -------------------------------------------- #
    hist_da = list(getattr(fin, "dep_amort", None) or []) if fin is not None else []
    hist_capex = list(getattr(fin, "capex", None) or []) if fin is not None else []

    if assumptions and is_num(assumptions.da_pct_revenue):
        da_pct = assumptions.da_pct_revenue
    else:
        # An all-zero history is the providers' gap filler, not a real 0%.
        da_pct = None if _all_zero(hist_da) else _hist_ratio_mean(hist_da, hist_revenue)
        if da_pct is None:
            da_pct = 0.0
            notes.append("D&A %revenue unavailable; defaulting to 0")

    if assumptions and is_num(assumptions.capex_pct_revenue):
        capex_pct = assumptions.capex_pct_revenue
    else:
        capex_pct = None if _all_zero(hist_capex) else _hist_ratio_mean(hist_capex, hist_revenue)
        if capex_pct is None:
            capex_pct = 0.0
            notes.append("capex %revenue unavailable; defaulting to 0")

    # Incremental NWC as a % of the revenue *change*.
    if assumptions and is_num(assumptions.nwc_pct_revenue):
        nwc_pct = assumptions.nwc_pct_revenue
    else:
        # Derive from history: pooled sum(dNWC_i) / sum(dRevenue_i), so a single
        # near-flat revenue year cannot dominate. change_in_nwc[i] aligns with
        # revenue[i]. Outside [0, 1] -> treat as no usable signal -> 0.
        hist_dnwc = list(getattr(fin, "change_in_nwc", None) or []) if fin is not None else []
        nwc_pct = incremental_ratio(hist_dnwc, hist_revenue)
        if nwc_pct is None or nwc_pct < 0 or nwc_pct > 1:
            # Unstable/implausible incremental ratio -> assume zero working-capital drag.
            if nwc_pct is not None:
                notes.append(f"derived dNWC/dRevenue {nwc_pct:.3f} implausible; using 0")
            nwc_pct = 0.0

    da = [rev * da_pct for rev in revenue]
    capex = [rev * capex_pct for rev in revenue]

    # dNWC_t = (revenue_t - revenue_{t-1}) * nwc_pct; t=0 uses base_revenue.
    dnwc: list[float] = []
    prev_rev = base_revenue
    for rev in revenue:
        dnwc.append((rev - prev_rev) * nwc_pct)
        prev_rev = rev

    # FCFF_t = NOPAT_t + D&A_t - Capex_t - dNWC_t
    fcff = [nopat[i] + da[i] - capex[i] - dnwc[i] for i in range(n)]

    # ----- 6) discounting -------------------------------------------------- #
    mid_year = bool(assumptions.mid_year_convention) if assumptions else True
    # Exponent for explicit year t (1-indexed): t-0.5 mid-year, else t.
    exponents = [(t - 0.5) if mid_year else float(t) for t in range(1, n + 1)]
    discount_factors = [1.0 / (1.0 + w) ** e for e in exponents]
    pv_fcff = [fcff[i] * discount_factors[i] for i in range(n)]

    # ----- 7) terminal value ---------------------------------------------- #
    # An unknown method, or exit_multiple without a multiple, falls back to Gordon
    # (with a note) so we still produce a number.
    terminal_method, method_note = resolve_terminal_method(assumptions)
    if method_note:
        notes.append(method_note)
    fcff_n = fcff[-1] if fcff else 0.0
    ebitda_n = (ebit[-1] + da[-1]) if (ebit and da) else 0.0

    g_used = terminal_growth
    if terminal_method == "exit_multiple":
        terminal_value = ebitda_n * assumptions.exit_ev_ebitda
    else:
        # Require WACC - g >= MAX_TERMINAL_GROWTH_VS_WACC; clamp g if violated.
        if w - g_used < config.MAX_TERMINAL_GROWTH_VS_WACC:
            clamped = w - config.MAX_TERMINAL_GROWTH_VS_WACC
            notes.append(
                f"terminal growth {g_used:.4f} too close to WACC {w:.4f}; "
                f"clamped to {clamped:.4f}"
            )
            g_used = clamped
        denom = w - g_used
        if denom <= 0:
            # Should not happen after clamping, but guard divide-by-zero anyway.
            terminal_value = 0.0
            notes.append("WACC-g non-positive after clamp; terminal value set to 0")
        else:
            terminal_value = fcff_n * (1.0 + g_used) / denom

    # Discount the TV. A Gordon TV is a perpetuity valued as of year N and shares
    # the final explicit flow's timing (N-0.5 under mid-year, else N). An
    # exit-multiple TV is a point-in-time, year-end sale value (EBITDA_N * mult),
    # so it is always discounted at the FULL period N regardless of mid-year.
    if terminal_method == "exit_multiple":
        tv_exponent = float(n)
        tv_exponent_rationale = (
            "exit-multiple TV is a year-end sale value; discounted at full period N"
        )
    else:
        tv_exponent = (n - 0.5) if mid_year else float(n)
        tv_exponent_rationale = (
            "Gordon TV shares the final explicit flow's timing "
            "(N-0.5 under mid-year, else N)"
        )
    tv_discount_factor = 1.0 / (1.0 + w) ** tv_exponent
    pv_terminal = terminal_value * tv_discount_factor

    # ----- 8) bridge to equity & implied price ---------------------------- #
    enterprise_value = sum(pv_fcff) + pv_terminal

    # net_debt = total_debt - cash (each missing component assumed 0 on its own);
    # add minority interest & preferred to bridge from enterprise to common equity.
    net_debt, bridge_notes = net_debt_parts(bs)
    notes.extend(bridge_notes)
    minority = getattr(bs, "minority_interest", 0.0) if bs is not None else 0.0
    preferred = getattr(bs, "preferred_equity", 0.0) if bs is not None else 0.0
    minority = minority if is_num(minority) else 0.0
    preferred = preferred if is_num(preferred) else 0.0

    total_claims = net_debt + minority + preferred
    equity_value = enterprise_value - total_claims

    # Shares: prefer live market shares outstanding, else latest diluted shares.
    shares = getattr(market, "shares_outstanding", None) if market is not None else None
    if not is_num(shares) or shares <= 0:
        shares = _latest(getattr(fin, "diluted_shares", None) or [] if fin is not None else [])
        if is_num(shares) and shares > 0:
            notes.append("market shares_outstanding unavailable; using latest diluted_shares")
    implied_price = safe_div(equity_value, shares)
    if implied_price is None:
        implied_price = 0.0
        notes.append("share count unavailable; implied price set to 0")
        shares = shares if is_num(shares) else 0.0

    cur_price = current_price if is_num(current_price) else (
        getattr(market, "price", None) if market is not None else None)
    upside = safe_div(implied_price, cur_price)
    upside = (upside - 1.0) if upside is not None else 0.0

    assumptions_dict = {
        "forecast_years": n,
        "revenue_growth_path": list(growth_path),
        "base_revenue": base_revenue,
        "ebit_margin_path": list(margin_path),
        "start_ebit_margin": start_margin,
        "target_ebit_margin": target_margin,
        "tax_rate": tax,
        "tax_source": tax_source,
        "da_pct_revenue": da_pct,
        "capex_pct_revenue": capex_pct,
        "nwc_pct_revenue": nwc_pct,
        "terminal_method": terminal_method,
        "terminal_growth": terminal_growth,
        "terminal_growth_used": g_used,
        "exit_ev_ebitda": (assumptions.exit_ev_ebitda if assumptions else None),
        "ebitda_terminal": ebitda_n,
        "mid_year_convention": mid_year,
        # Document the discounting choice explicitly for downstream exporters.
        "discount_exponents": list(exponents),
        "terminal_discount_exponent": tv_exponent,
        "terminal_discount_exponent_rationale": tv_exponent_rationale,
        "wacc": w,
        "notes": notes,
    }

    return DCFResult(
        wacc=wacc_result,
        years=list(range(1, n + 1)),
        revenue=revenue,
        ebit=ebit,
        nopat=nopat,
        fcff=fcff,
        discount_factors=discount_factors,
        pv_fcff=pv_fcff,
        terminal_value=terminal_value,
        pv_terminal=pv_terminal,
        enterprise_value=enterprise_value,
        net_debt=net_debt,
        equity_value=equity_value,
        shares=shares if is_num(shares) else 0.0,
        implied_price=implied_price,
        current_price=cur_price if is_num(cur_price) else 0.0,
        upside=upside,
        assumptions=assumptions_dict,
    )
