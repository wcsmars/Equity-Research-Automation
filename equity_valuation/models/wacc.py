"""Weighted-average cost of capital (WACC) and its building blocks.

This module derives the discount rate used by the unlevered FCFF DCF:

  * `effective_tax_rate` — a robust, history-based effective tax rate.
  * `compute_wacc`       — CAPM cost of equity + a cost of debt, blended on
                           market-value weights.

Everything here is pure-Python (stdlib + the package's own utils). All rates are
decimals (8% -> 0.08) and all monetary inputs are absolute units (not millions),
matching the conventions in ``schemas.py``.
"""

from __future__ import annotations

from ..schemas import AnnualFinancials, CompanyData, MacroAssumptions, WACCResult
from ..utils import is_num, median, safe_div
from .. import config


# --------------------------------------------------------------------------- #
#  Effective tax rate
# --------------------------------------------------------------------------- #
def effective_tax_rate(fin: AnnualFinancials, fallback: float) -> float:
    """Median historical effective tax rate (tax_expense / pretax_income).

    Only years with *positive* pre-tax income are used — a negative or zero EBT
    makes the ratio meaningless (and would let a tax benefit produce a nonsense
    negative rate). The median is robust to one-off items. The result is clamped
    to ``[config.MIN_EFFECTIVE_TAX_RATE, config.MAX_EFFECTIVE_TAX_RATE]``.

    If no usable year exists (no positive-EBT year, or the financials are
    missing/None), the provided ``fallback`` is returned unchanged.
    """
    # Guard against a malformed/empty financials object.
    if fin is None:
        return fallback

    pretax = getattr(fin, "pretax_income", None) or []
    taxes = getattr(fin, "tax_expense", None) or []

    rates: list[float] = []
    # Pair up tax/pretax year-by-year; only keep positive-EBT years.
    for tax, ebt in zip(taxes, pretax):
        if not is_num(tax) or not is_num(ebt) or ebt <= 0:
            continue
        ratio = safe_div(tax, ebt)
        if ratio is not None:
            rates.append(ratio)

    med = median(rates)
    if med is None:
        # No usable history — defer to the caller's fallback (often the macro
        # marginal rate or config.DEFAULT_MARGINAL_TAX_RATE).
        return fallback

    # Clamp into the sane band defined in config.
    lo, hi = config.MIN_EFFECTIVE_TAX_RATE, config.MAX_EFFECTIVE_TAX_RATE
    return max(lo, min(hi, med))


# --------------------------------------------------------------------------- #
#  WACC
# --------------------------------------------------------------------------- #
def compute_wacc(company: CompanyData, macro: MacroAssumptions) -> WACCResult:
    """Compute WACC for ``company`` under the macro assumptions.

    Cost of equity (CAPM):   ke = rf + beta * ERP
        beta comes from market data if available, else ``config.DEFAULT_BETA``.

    Pre-tax cost of debt (first sane source wins):
        1. ``macro.pretax_cost_of_debt`` if explicitly supplied.
        2. interest_expense(latest) / total_debt — but only accepted if it lands
           in the plausible band [0.01, 0.15] (filters out distorted readings
           when debt is tiny or interest is mismatched).
        3. ``rf + config.DEFAULT_CREDIT_SPREAD`` as the last-resort proxy.

    After-tax cost of debt:  kd_at = kd_pretax * (1 - tax).

    Weights use MARKET values: E = market_cap, D = total_debt (book proxy).
        w_e = E / (E + D),  w_d = D / (E + D).  If E + D == 0 -> treat as
        all-equity (w_e = 1, w_d = 0).

    WACC = w_e * ke + w_d * kd_at.

    Never raises: missing fields degrade to documented defaults and every input
    is recorded in ``WACCResult.detail`` (including a ``notes`` list).
    """
    notes: list[str] = []

    # --- macro inputs (guard each) ----------------------------------------- #
    rf = macro.risk_free_rate if (macro is not None and is_num(macro.risk_free_rate)) \
        else config.DEFAULT_RISK_FREE_RATE
    erp = macro.equity_risk_premium if (macro is not None and is_num(macro.equity_risk_premium)) \
        else config.DEFAULT_EQUITY_RISK_PREMIUM

    market = getattr(company, "market", None)
    fin = getattr(company, "financials", None)
    bs = getattr(company, "balance_sheet", None)

    # --- beta -------------------------------------------------------------- #
    beta = getattr(market, "beta", None) if market is not None else None
    if not is_num(beta):
        beta = config.DEFAULT_BETA
        notes.append(f"beta unavailable; using DEFAULT_BETA={config.DEFAULT_BETA}")

    # --- cost of equity (CAPM) --------------------------------------------- #
    cost_of_equity = rf + beta * erp

    # --- effective tax rate ------------------------------------------------ #
    # Prefer an explicit macro tax rate; otherwise derive from history with the
    # statutory marginal rate as the ultimate fallback.
    if macro is not None and is_num(macro.tax_rate):
        tax = macro.tax_rate
        tax_source = "macro.tax_rate"
    else:
        tax = effective_tax_rate(fin, config.DEFAULT_MARGINAL_TAX_RATE)
        tax_source = "effective (historical)"

    # --- pre-tax cost of debt ---------------------------------------------- #
    total_debt = getattr(bs, "total_debt", None) if bs is not None else None
    if not is_num(total_debt) or total_debt < 0:
        total_debt = 0.0

    interest_latest = None
    if fin is not None:
        ints = getattr(fin, "interest_expense", None) or []
        if ints and is_num(ints[-1]):
            interest_latest = abs(ints[-1])  # stored positive, but be safe

    if macro is not None and is_num(macro.pretax_cost_of_debt):
        kd_pretax = macro.pretax_cost_of_debt
        kd_source = "macro.pretax_cost_of_debt"
    else:
        derived = safe_div(interest_latest, total_debt)
        if derived is not None and 0.01 <= derived <= 0.15:
            kd_pretax = derived
            kd_source = "interest_expense/total_debt"
        else:
            kd_pretax = rf + config.DEFAULT_CREDIT_SPREAD
            kd_source = "rf + DEFAULT_CREDIT_SPREAD"
            if derived is not None:
                notes.append(
                    f"derived cost of debt {derived:.4f} outside [0.01,0.15]; "
                    f"using rf+spread"
                )
            else:
                notes.append("cost of debt not derivable; using rf+spread")

    after_tax_cost_of_debt = kd_pretax * (1.0 - tax)

    # --- market-value weights ---------------------------------------------- #
    equity_value = getattr(market, "market_cap", None) if market is not None else None
    if not is_num(equity_value) or equity_value < 0:
        equity_value = 0.0
        notes.append("market_cap unavailable; equity weight may be unreliable")

    total_cap = equity_value + total_debt
    if total_cap <= 0:
        # Degenerate: no capital structure information -> assume all equity.
        weight_equity = 1.0
        weight_debt = 0.0
        notes.append("E+D == 0; defaulting to all-equity weights")
    else:
        weight_equity = equity_value / total_cap
        weight_debt = total_debt / total_cap

    # --- blend ------------------------------------------------------------- #
    wacc = weight_equity * cost_of_equity + weight_debt * after_tax_cost_of_debt

    detail = {
        "risk_free_rate": rf,
        "equity_risk_premium": erp,
        "beta": beta,
        "cost_of_equity": cost_of_equity,
        "tax_rate": tax,
        "tax_source": tax_source,
        "pretax_cost_of_debt": kd_pretax,
        "cost_of_debt_source": kd_source,
        "after_tax_cost_of_debt": after_tax_cost_of_debt,
        "equity_value": equity_value,
        "total_debt": total_debt,
        "weight_equity": weight_equity,
        "weight_debt": weight_debt,
        "interest_expense_latest": interest_latest,
        "wacc": wacc,
        "notes": notes,
    }

    return WACCResult(
        cost_of_equity=cost_of_equity,
        after_tax_cost_of_debt=after_tax_cost_of_debt,
        weight_equity=weight_equity,
        weight_debt=weight_debt,
        wacc=wacc,
        beta=beta,
        detail=detail,
    )
