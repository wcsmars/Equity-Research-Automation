"""Dividend-Discount (DDM) and Free-Cash-Flow-to-Equity (FCFE) valuation models.

These are *levered* / equity-side counterparts to the unlevered FCFF DCF. They
discount cash flows that accrue directly to equity holders at the cost of equity
(CAPM), so there is no WACC and no enterprise-value bridge -- the output is an
implied price per share directly.

Design notes:
  * This module is deliberately INDEPENDENT of ``models/dcf.py``. The few small
    helpers it needs (revenue-growth path, ratio-of-revenue projections) are
    re-derived locally on top of ``utils`` so the two model families can evolve
    without coupling.
  * Every access to a financial field is guarded -- the providers frequently
    leave series sparse, zero-filled, or None, and the models must degrade
    gracefully and record a human-readable note rather than crash.
  * Money is in absolute units; rates are decimals; annual series run
    OLDEST -> NEWEST (``[-1]`` is the most recent fiscal year).
"""

from __future__ import annotations

from typing import Optional

from .. import config
from ..schemas import (
    AnnualFinancials,
    CompanyData,
    DDMAssumptions,
    DDMResult,
    FCFEResult,
    MacroAssumptions,
)
from ..utils import cagr, fade_path, is_num, mean, safe_div

# Minimum spread required between the cost of equity and a perpetual growth rate
# for a Gordon-style terminal/perpetuity to be finite and well-behaved. The
# interface mandates ke - g >= 0.005; if an input growth rate violates it we clamp
# the growth rate down so the spread is restored (recording a note).
_MIN_KE_G_SPREAD = 0.005


# --------------------------------------------------------------------------- #
#  Cost of equity (CAPM)
# --------------------------------------------------------------------------- #
def cost_of_equity(company: CompanyData, macro: MacroAssumptions) -> float:
    """CAPM cost of equity: ``ke = rf + beta * ERP``.

    Beta is taken from live market data when available and finite, otherwise it
    falls back to ``config.DEFAULT_BETA``. Risk-free rate and equity-risk-premium
    come from the macro assumptions (both decimals).
    """
    rf = macro.risk_free_rate if is_num(macro.risk_free_rate) else config.DEFAULT_RISK_FREE_RATE
    erp = (
        macro.equity_risk_premium
        if is_num(macro.equity_risk_premium)
        else config.DEFAULT_EQUITY_RISK_PREMIUM
    )

    beta = None
    market = getattr(company, "market", None)
    if market is not None:
        beta = getattr(market, "beta", None)
    if not is_num(beta):
        beta = config.DEFAULT_BETA

    return rf + beta * erp


# --------------------------------------------------------------------------- #
#  Small shared helpers (kept local -- no dependency on models/dcf.py)
# --------------------------------------------------------------------------- #
def _shares(company: CompanyData) -> Optional[float]:
    """Best-effort share count: market shares outstanding, else latest diluted."""
    market = getattr(company, "market", None)
    if market is not None:
        so = getattr(market, "shares_outstanding", None)
        if is_num(so) and so > 0:
            return float(so)
    fin = getattr(company, "financials", None)
    if fin is not None:
        diluted = getattr(fin, "diluted_shares", None) or []
        for v in reversed(diluted):  # newest first; take the latest sane value
            if is_num(v) and v > 0:
                return float(v)
    return None


def _hist_revenue_cagr(fin: AnnualFinancials) -> Optional[float]:
    """Historical revenue CAGR over the available (positive) annual series."""
    rev = [v for v in (getattr(fin, "revenue", None) or []) if is_num(v)]
    if len(rev) < 2:
        return None
    periods = len(rev) - 1
    return cagr(rev[0], rev[-1], periods)


def _revenue_growth_path(fin: AnnualFinancials, terminal_growth: float, n: int) -> list[float]:
    """Per-year revenue growth, fading the historical CAGR toward terminal growth.

    Mirrors the DCF's growth derivation (but re-implemented locally): start from
    the historical revenue CAGR, clamp to the configured near-term band, then
    linearly fade to ``terminal_growth`` over ``n`` forecast years. If no usable
    history exists, the whole path is the terminal growth (a conservative flat
    assumption).
    """
    if n <= 0:
        return []
    base = _hist_revenue_cagr(fin)
    if not is_num(base):
        base = terminal_growth
    # Clamp the near-term growth into the configured sane band.
    base = max(config.DEFAULT_REVENUE_GROWTH_FLOOR, min(config.DEFAULT_REVENUE_GROWTH_CAP, base))
    return fade_path(base, terminal_growth, n)


def _ratio_of_revenue(series: Optional[list], revenue: Optional[list]) -> Optional[float]:
    """Average ratio of a flow series to revenue over aligned positive-revenue years.

    Used to turn D&A / capex into a forward % of revenue from history. Returns the
    mean of the per-year ratios (None if nothing usable).
    """
    series = series or []
    revenue = revenue or []
    ratios: list[float] = []
    for s, r in zip(series, revenue):
        if is_num(s) and is_num(r) and r > 0:
            ratios.append(s / r)
    return mean(ratios)


def _latest_net_margin(fin: AnnualFinancials) -> Optional[float]:
    """Latest net income / revenue, guarding None/zero revenue."""
    ni = getattr(fin, "net_income", None) or []
    rev = getattr(fin, "revenue", None) or []
    if not ni or not rev:
        return None
    return safe_div(ni[-1], rev[-1])


# --------------------------------------------------------------------------- #
#  Dividend Discount Model
# --------------------------------------------------------------------------- #
def _sustainable_growth(fin: AnnualFinancials, company: CompanyData) -> Optional[float]:
    """Sustainable growth = ROE * retention ratio.

    ROE = latest net income / book equity. Retention = 1 - payout, where payout =
    dividends paid / net income (clamped to [0, 1]). Returns None if inputs are
    unusable (e.g. non-positive equity or net income).
    """
    ni = (getattr(fin, "net_income", None) or [None])[-1]
    equity = getattr(getattr(company, "balance_sheet", None), "total_equity", None)
    roe = safe_div(ni, equity)
    if not is_num(roe) or not is_num(ni) or ni <= 0:
        return None

    div_paid = (getattr(fin, "dividends_paid", None) or [None])[-1]
    payout = safe_div(div_paid, ni)
    if not is_num(payout):
        payout = 0.0
    payout = max(0.0, min(1.0, payout))
    retention = 1.0 - payout
    return roe * retention


def _dividend_cagr(fin: AnnualFinancials) -> Optional[float]:
    """CAGR of total dividends paid over the available positive history."""
    div = [v for v in (getattr(fin, "dividends_paid", None) or []) if is_num(v) and v > 0]
    if len(div) < 2:
        return None
    return cagr(div[0], div[-1], len(div) - 1)


def run_ddm(
    company: CompanyData,
    macro: MacroAssumptions,
    assumptions: DDMAssumptions,
    current_price: float,
) -> Optional[DDMResult]:
    """Dividend Discount Model -> implied price per share.

    Returns ``None`` (no crash) for non-dividend-payers, i.e. when
    ``market.dividend_per_share`` is None or 0.

    Supported methods (``assumptions.method``):
      * ``gordon``    -- single-stage Gordon growth at the terminal rate.
      * ``two_stage`` -- ``high_growth_years`` of high growth, then a Gordon
                         perpetuity at the terminal rate.
      * ``h_model``   -- linearly-declining growth from an initial high rate to
                         the terminal rate (closed-form H-model).

    In every case the cost of equity is CAPM. Where a perpetuity is taken we
    require ``ke - g >= 0.005`` and clamp ``g`` down if necessary, recording the
    adjustment in ``detail['notes']``.
    """
    notes: list[str] = []
    market = getattr(company, "market", None)
    d0 = getattr(market, "dividend_per_share", None) if market is not None else None

    # Non-dividend payer -> DDM is inapplicable. Return None per the contract.
    if not is_num(d0) or d0 == 0:
        return None
    d0 = float(d0)

    ke = cost_of_equity(company, macro)
    g_terminal = assumptions.terminal_growth if is_num(assumptions.terminal_growth) else 0.0
    method = (assumptions.method or "two_stage").lower()
    fin = getattr(company, "financials", None)

    detail: dict = {
        "method": method,
        "cost_of_equity": ke,
        "D0": d0,
        "terminal_growth": g_terminal,
        "notes": notes,
    }

    # --- helper: clamp a perpetual growth rate so ke - g >= the minimum spread -- #
    def _clamp_terminal_g(g: float, label: str) -> float:
        if ke - g < _MIN_KE_G_SPREAD:
            new_g = ke - _MIN_KE_G_SPREAD
            notes.append(
                f"{label} growth {g:.4f} too close to ke {ke:.4f}; clamped to {new_g:.4f}."
            )
            return new_g
        return g

    # ------------------------------------------------------------------ gordon
    if method == "gordon":
        g = _clamp_terminal_g(g_terminal, "Gordon")
        denom = ke - g
        price = safe_div(d0 * (1.0 + g), denom)
        detail["growth"] = g
        detail["implied_price"] = price
        if not is_num(price) or price < 0:
            notes.append("Gordon DDM produced a non-positive/undefined price.")
            price = 0.0
        return DDMResult(method="gordon", implied_price=float(price), cost_of_equity=ke, detail=detail)

    # --------------------------------------------------------------- h_model
    if method == "h_model":
        # Closed-form H-model: P = [D0*(1+g) + D0*H_half*(gh - g)] / (ke - g)
        # where H_half = high_growth_years / 2 is the half-life of the linear fade.
        gh = _initial_high_growth(assumptions, fin, company, ke, notes)
        g = _clamp_terminal_g(g_terminal, "H-model terminal")
        if (assumptions.high_growth_years or 0) > 0:
            h_years = assumptions.high_growth_years
        else:
            h_years = 5
            notes.append("high_growth_years missing/0; defaulted H-model horizon to 5 years.")
        h_half = h_years / 2.0
        denom = ke - g
        numer = d0 * (1.0 + g) + d0 * h_half * (gh - g)
        price = safe_div(numer, denom)
        detail.update({"high_growth": gh, "terminal_growth_used": g, "H_half": h_half})
        detail["implied_price"] = price
        if not is_num(price) or price < 0:
            notes.append("H-model DDM produced a non-positive/undefined price.")
            price = 0.0
        return DDMResult(method="h_model", implied_price=float(price), cost_of_equity=ke, detail=detail)

    # -------------------------------------------------------------- two_stage
    # (default; also catches any unrecognised method string)
    if method != "two_stage":
        notes.append(f"Unknown DDM method '{method}'; defaulting to two_stage.")
        method = "two_stage"
        detail["method"] = method

    gh = _initial_high_growth(assumptions, fin, company, ke, notes)
    h_years = assumptions.high_growth_years if (assumptions.high_growth_years or 0) > 0 else 5
    g = _clamp_terminal_g(g_terminal, "Two-stage terminal")

    # Stage 1: explicit dividends grown at gh, discounted at ke.
    stage_pvs: list[float] = []
    dividends: list[float] = []
    d_prev = d0
    pv_stage1 = 0.0
    for t in range(1, h_years + 1):
        d_t = d_prev * (1.0 + gh)
        df = 1.0 / ((1.0 + ke) ** t)
        pv = d_t * df
        dividends.append(d_t)
        stage_pvs.append(pv)
        pv_stage1 += pv
        d_prev = d_t

    # Stage 2: Gordon perpetuity on the dividend at the end of the high-growth
    # phase, valued at year H then discounted back H years.
    d_h = d_prev  # dividend at the end of year H (the last grown dividend)
    tv = safe_div(d_h * (1.0 + g), ke - g)
    if not is_num(tv):
        tv = 0.0
        notes.append("Two-stage terminal value undefined; set to 0.")
    pv_terminal = tv / ((1.0 + ke) ** h_years)

    price = pv_stage1 + pv_terminal
    if not is_num(price) or price < 0:
        notes.append("Two-stage DDM produced a non-positive/undefined price.")
        price = 0.0

    detail.update(
        {
            "high_growth": gh,
            "high_growth_years": h_years,
            "terminal_growth_used": g,
            "dividends": dividends,
            "stage1_pvs": stage_pvs,
            "pv_stage1": pv_stage1,
            "terminal_value": tv,
            "pv_terminal": pv_terminal,
            "implied_price": price,
        }
    )
    return DDMResult(method="two_stage", implied_price=float(price), cost_of_equity=ke, detail=detail)


def _initial_high_growth(
    assumptions: DDMAssumptions,
    fin: Optional[AnnualFinancials],
    company: CompanyData,
    ke: float,
    notes: list[str],
) -> float:
    """Resolve the stage-1 (high) growth rate for two-stage / H-model DDM.

    Precedence:
      1. ``assumptions.high_growth_rate`` if explicitly supplied.
      2. else ``min(sustainable growth = ROE*retention, dividend CAGR)`` over the
         candidates that are actually computable.
      3. else fall back to the terminal growth (a conservative flat assumption).

    The result is always clamped to strictly below the cost of equity (a high
    growth rate >= ke would make the dividend stream out-explode the discount and
    is economically implausible for a mature company).
    """
    gh = assumptions.high_growth_rate
    if not is_num(gh):
        candidates: list[float] = []
        if fin is not None:
            sg = _sustainable_growth(fin, company)
            if is_num(sg):
                candidates.append(sg)
            dcg = _dividend_cagr(fin)
            if is_num(dcg):
                candidates.append(dcg)
        if candidates:
            gh = min(candidates)
        else:
            gh = assumptions.terminal_growth if is_num(assumptions.terminal_growth) else 0.0
            notes.append(
                "No ROE/retention or dividend-CAGR signal; high growth set to terminal growth."
            )

    if not is_num(gh):
        gh = 0.0

    # Clamp strictly below ke so the stage-1 stream stays well-behaved.
    cap = ke - _MIN_KE_G_SPREAD
    if gh >= cap:
        notes.append(f"High growth {gh:.4f} >= ke margin; clamped to {cap:.4f}.")
        gh = cap
    return gh


# --------------------------------------------------------------------------- #
#  Free Cash Flow to Equity model
# --------------------------------------------------------------------------- #
def run_fcfe(
    company: CompanyData,
    macro: MacroAssumptions,
    assumptions: DDMAssumptions,
    current_price: float,
) -> FCFEResult:
    """Levered FCFE DCF -> implied price per share.

    FCFE definition (levered free cash flow available to equity holders):

        FCFE_t = NetIncome_t + D&A_t - Capex_t - ΔNWC_t + ΔDebt_t

    ΔDebt CHOICE (documented): we assume the firm maintains a constant
    debt-to-revenue ratio, so net new borrowing grows the debt balance in line
    with revenue: ΔDebt_t = total_debt * (revenue_t / revenue_{t-1} - 1). This is
    a standard "constant capital structure" simplification when no explicit debt
    schedule is available. If the current debt balance or base revenue is
    unavailable, ΔDebt falls back to 0 (a note is recorded). This keeps leverage
    neutral rather than assuming aggressive re-levering.

    Projection mechanics:
      * Revenue grows along the historical-CAGR path faded to terminal growth
        (same derivation philosophy as the DCF, re-implemented locally).
      * NetIncome_t = latest net margin * projected revenue_t.
      * D&A_t / Capex_t = (historical % of revenue) * revenue_t.
      * ΔNWC_t = (historical avg ΔNWC / Δrevenue) * Δrevenue_t (small/zero if the
        history is unstable -- ΔNWC series is often zero-filled by the provider).
      * Discount each FCFE_t at the cost of equity (CAPM).
      * Terminal value: Gordon growth on FCFE_N at terminal_growth, discounted N
        years (require ke - g >= 0.005, clamp g if needed).
      * equity_value = sum(PV_FCFE) + PV_terminal; implied_price = equity/shares.
    """
    notes: list[str] = []
    ke = cost_of_equity(company, macro)
    fin = getattr(company, "financials", None)
    bs = getattr(company, "balance_sheet", None)

    years_out = assumptions.forecast_years if (assumptions.forecast_years or 0) > 0 else 5
    g_terminal = assumptions.terminal_growth if is_num(assumptions.terminal_growth) else 0.0
    shares = _shares(company)

    detail: dict = {
        "cost_of_equity": ke,
        "terminal_growth": g_terminal,
        "delta_debt_policy": "constant debt/revenue ratio (debt grows with revenue)",
        "notes": notes,
    }

    # --- guard: we need at least a revenue base and a net margin to project. --- #
    base_revenue = None
    if fin is not None:
        rev_hist = [v for v in (getattr(fin, "revenue", None) or []) if is_num(v)]
        if rev_hist:
            base_revenue = rev_hist[-1]
    net_margin = _latest_net_margin(fin) if fin is not None else None

    # Forward fiscal-year labels for the projection horizon.
    last_fy = None
    if fin is not None:
        fys = [int(y) for y in (getattr(fin, "fiscal_years", None) or []) if is_num(y)]
        if fys:
            last_fy = fys[-1]
    proj_years = (
        [last_fy + i for i in range(1, years_out + 1)]
        if last_fy is not None
        else list(range(1, years_out + 1))
    )

    # If we cannot even establish a revenue base or margin, return a graceful,
    # zero-valued result rather than crashing.
    if not is_num(base_revenue) or base_revenue <= 0 or not is_num(net_margin):
        notes.append("Insufficient revenue/margin history to project FCFE; returning zeros.")
        zeros = [0.0] * years_out
        return FCFEResult(
            years=proj_years,
            fcfe=zeros,
            pv_fcfe=zeros,
            terminal_value=0.0,
            pv_terminal=0.0,
            equity_value=0.0,
            shares=shares if is_num(shares) else 0.0,
            implied_price=0.0,
            current_price=current_price,
            cost_of_equity=ke,
            detail=detail,
        )

    # --- per-revenue ratios from history (held flat over the horizon) --------- #
    rev_series = getattr(fin, "revenue", None)
    da_pct = _ratio_of_revenue(getattr(fin, "dep_amort", None), rev_series)
    capex_pct = _ratio_of_revenue(getattr(fin, "capex", None), rev_series)
    if not is_num(da_pct):
        da_pct = 0.0
        notes.append("No usable D&A history; D&A set to 0% of revenue.")
    if not is_num(capex_pct):
        capex_pct = 0.0
        notes.append("No usable capex history; capex set to 0% of revenue.")

    # Incremental NWC as a fraction of the change in revenue (avg over history).
    nwc_pct_delta = _nwc_per_revenue_change(fin)
    if not is_num(nwc_pct_delta):
        nwc_pct_delta = 0.0
        notes.append("Unstable/absent ΔNWC history; incremental NWC set to 0% of Δrevenue.")

    # Current debt balance for the ΔDebt (debt grows with revenue) policy.
    total_debt = getattr(bs, "total_debt", None) if bs is not None else None
    if not is_num(total_debt):
        total_debt = 0.0
        notes.append("Total debt unavailable; ΔDebt set to 0 (no re-levering).")

    # Terminal growth used consistently as BOTH the fade endpoint of the growth
    # path AND the perpetuity growth, so FCFE_N and the Gordon terminal share one g.
    g_term = min(g_terminal, ke - _MIN_KE_G_SPREAD)
    if g_term < g_terminal:
        notes.append(
            f"Terminal growth {g_terminal:.4f} too close to/above ke {ke:.4f}; "
            f"clamped to {g_term:.4f} (used in both fade path and terminal value)."
        )

    growth_path = _revenue_growth_path(fin, g_term, years_out)
    detail["revenue_growth_path"] = growth_path
    detail["net_margin"] = net_margin
    detail["da_pct_revenue"] = da_pct
    detail["capex_pct_revenue"] = capex_pct
    detail["nwc_pct_delta_revenue"] = nwc_pct_delta

    # --- project the FCFE series --------------------------------------------- #
    revenues: list[float] = []
    fcfe: list[float] = []
    pv_fcfe: list[float] = []
    prev_rev = float(base_revenue)
    for t in range(1, years_out + 1):
        g = growth_path[t - 1] if (t - 1) < len(growth_path) else g_term
        rev_t = prev_rev * (1.0 + g)
        d_rev = rev_t - prev_rev

        ni_t = net_margin * rev_t
        da_t = da_pct * rev_t
        capex_t = capex_pct * rev_t
        dnwc_t = nwc_pct_delta * d_rev
        # ΔDebt: keep debt/revenue constant -> borrow in proportion to revenue growth.
        ddebt_t = total_debt * g if total_debt else 0.0

        fcfe_t = ni_t + da_t - capex_t - dnwc_t + ddebt_t
        df = 1.0 / ((1.0 + ke) ** t)
        pv_t = fcfe_t * df

        revenues.append(rev_t)
        fcfe.append(fcfe_t)
        pv_fcfe.append(pv_t)
        prev_rev = rev_t

    detail["revenue"] = revenues

    # --- terminal value: Gordon on FCFE_N (shares g_term with the fade path) -- #
    fcfe_n = fcfe[-1] if fcfe else 0.0
    tv = safe_div(fcfe_n * (1.0 + g_term), ke - g_term)
    if not is_num(tv):
        tv = 0.0
        notes.append("FCFE terminal value undefined; set to 0.")
    pv_terminal = tv / ((1.0 + ke) ** years_out)

    equity_value = sum(pv_fcfe) + pv_terminal
    implied_price = safe_div(equity_value, shares)
    if not is_num(implied_price):
        implied_price = 0.0
        notes.append("Share count unavailable; implied price set to 0.")

    detail["terminal_growth_used"] = g_term
    detail["equity_value"] = equity_value

    return FCFEResult(
        years=proj_years,
        fcfe=fcfe,
        pv_fcfe=pv_fcfe,
        terminal_value=float(tv),
        pv_terminal=float(pv_terminal),
        equity_value=float(equity_value),
        shares=float(shares) if is_num(shares) else 0.0,
        implied_price=float(implied_price),
        current_price=current_price,
        cost_of_equity=ke,
        detail=detail,
    )


def _nwc_per_revenue_change(fin: AnnualFinancials) -> Optional[float]:
    """Average ΔNWC as a fraction of the year-over-year change in revenue.

    The provider stores ``change_in_nwc`` already as the per-year increase in net
    working capital (positive = cash use). We pair each year's ΔNWC with that
    year's revenue change and average the ratios over the years where the revenue
    change is non-trivial. Returns None if there is no stable signal (the caller
    then defaults to 0, consistent with the DCF's treatment).
    """
    rev = getattr(fin, "revenue", None) or []
    nwc = getattr(fin, "change_in_nwc", None) or []
    if len(rev) < 2 or len(nwc) < 2:
        return None
    ratios: list[float] = []
    for i in range(1, min(len(rev), len(nwc))):
        if not (is_num(rev[i]) and is_num(rev[i - 1]) and is_num(nwc[i])):
            continue
        d_rev = rev[i] - rev[i - 1]
        # Ignore years with a negligible revenue change -- the ratio explodes and
        # is not informative about the structural NWC intensity.
        if abs(d_rev) < 1e-9:
            continue
        ratios.append(nwc[i] / d_rev)
    return mean(ratios)
