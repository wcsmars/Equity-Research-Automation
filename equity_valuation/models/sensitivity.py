"""Sensitivity analysis and football-field assembly.

Two public entry points:

  * ``dcf_sensitivity`` re-runs the unlevered FCFF DCF (``models.dcf.run_dcf``)
    across two 2-D grids, capturing the implied price per cell:
        Grid 1 — WACC  x  terminal growth   (Gordon terminal only)
        Grid 2 — EBIT margin  x  terminal growth
    Each cell is an independent DCF run on a *cloned* macro/assumptions pair
    (via ``dataclasses.replace``) so the base inputs are never mutated.

  * ``build_football_field`` collapses the populated ``ValuationReport`` into a
    list of ``FootballFieldRow`` low/base/high bars — one per valuation method
    that has usable inputs (52-week range, DCF, comps, DDM, FCFE).

Design notes:
  * Money is in absolute units; growth/margins/WACC are decimals (see schemas).
  * Nothing here ever raises on a per-cell modelling failure: a failed DCF cell
    stores ``float('nan')`` so the grid stays rectangular and the exporters can
    render a blank cell.
  * Row/column *values* are the ACTUAL resulting levels (e.g. the realized WACC
    read back off the returned ``DCFResult``), not the raw deltas, so the labels
    on the grid are economically meaningful. A cell the DCF could only price by
    changing its inputs (terminal g clamped below WACC, or a non-positive WACC
    replaced by the fallback rate) is stored as NaN rather than shown under a
    label it does not represent.

"""

from __future__ import annotations

from dataclasses import replace

from .. import config
from ..schemas import (
    CompanyData,
    DCFAssumptions,
    FootballFieldRow,
    MacroAssumptions,
    SensitivityResult,
)
from ..utils import is_num, median, net_debt_parts
from .dcf import resolve_terminal_method, run_dcf, start_ebit_margin


# --------------------------------------------------------------------------- #
#  Small internal helpers
# --------------------------------------------------------------------------- #
def _safe_implied_price(
    company: CompanyData,
    macro: MacroAssumptions,
    assumptions: DCFAssumptions,
    current_price: float,
):
    """Run one DCF cell, returning (implied_price, realized_wacc).

    Never raises: on any failure both elements degrade to ``float('nan')`` so the
    caller can keep building a rectangular grid. The price is also NaN when the
    DCF had to clamp the cell's terminal growth or replace a non-positive WACC,
    since the cell would then not be priced at its row/column labels.
    """
    try:
        result = run_dcf(company, macro, assumptions, current_price)
    except Exception:  # defensive: e.g. no positive revenue base
        return float("nan"), float("nan")

    price = getattr(result, "implied_price", None)
    price = float(price) if is_num(price) else float("nan")

    # Read the realized WACC back off the result so the row label reflects the
    # level the model actually used (rf bump propagates through CAPM + weights).
    realized_wacc = float("nan")
    wacc_obj = getattr(result, "wacc", None)
    detail = getattr(wacc_obj, "detail", None) or {}
    if wacc_obj is not None:
        w = getattr(wacc_obj, "wacc", None)
        if "wacc_computed" in detail:
            # Discounted at the fallback rate: keep the CAPM WACC as the (ordered)
            # row label and blank the cell.
            w = detail["wacc_computed"]
            price = float("nan")
        if is_num(w):
            realized_wacc = float(w)

    a = getattr(result, "assumptions", None) or {}
    g_req, g_used = a.get("terminal_growth"), a.get("terminal_growth_used")
    if is_num(g_req) and is_num(g_used) and g_used != g_req:
        price = float("nan")  # g clamped to WACC - gap: not the column's growth
    return price, realized_wacc


def _base_latest_ebit_margin(company: CompanyData) -> float:
    """Latest historical EBIT / revenue, or NaN if it can't be computed.

    Uses the DCF's own start-margin derivation so the margin grid is centered on
    the same starting point the model uses.
    """
    fin = getattr(company, "financials", None)
    if fin is None:
        return float("nan")
    revenue = list(getattr(fin, "revenue", None) or [])
    rev_latest = revenue[-1] if revenue else None
    if not is_num(rev_latest) or rev_latest <= 0:
        return float("nan")
    margin, _ = start_ebit_margin(fin, revenue, rev_latest)
    return float(margin) if is_num(margin) else float("nan")


# --------------------------------------------------------------------------- #
#  Public API — sensitivity grids
# --------------------------------------------------------------------------- #
def dcf_sensitivity(
    company: CompanyData,
    macro: MacroAssumptions,
    assumptions: DCFAssumptions,
    current_price: float,
) -> list[SensitivityResult]:
    """Build the WACCxgrowth and marginxgrowth implied-price sensitivity grids.

    Both grids force the Gordon terminal method (``terminal_method='gordon'``)
    so terminal-growth shifts have a well-defined effect; the exit-multiple
    branch ignores terminal growth entirely. When the headline DCF uses an exit
    multiple, the grid titles say so, since their centre cell is then the
    Gordon price rather than the headline.

    Returns a list of up to two ``SensitivityResult`` objects. A grid that can't
    be built at all (e.g. base margin unknown) is still returned, populated with
    ``float('nan')`` cells, so downstream consumers see consistent structure.
    """
    results: list[SensitivityResult] = []

    growth_deltas = list(config.SENSITIVITY_GROWTH_DELTAS)
    wacc_deltas = list(config.SENSITIVITY_WACC_DELTAS)
    margin_deltas = list(config.SENSITIVITY_MARGIN_DELTAS)

    base_growth = getattr(assumptions, "terminal_growth", None)
    if not is_num(base_growth):
        base_growth = config.DEFAULT_TERMINAL_GROWTH
    base_growth = float(base_growth)

    base_rf = getattr(macro, "risk_free_rate", None)
    if not is_num(base_rf):
        base_rf = config.DEFAULT_RISK_FREE_RATE
    base_rf = float(base_rf)

    # The actual terminal-growth levels are shared across both grids' columns.
    growth_levels = [base_growth + d for d in growth_deltas]

    # Label the grids as Gordon-based when the headline DCF is not.
    headline_method, _ = resolve_terminal_method(assumptions)
    title_suffix = (" (Gordon terminal; headline DCF uses exit multiple)"
                    if headline_method == "exit_multiple" else "")

    # ----------------------------------------------------------------------- #
    # Grid 1 — WACC (rows) x terminal growth (cols)
    #
    # We shift WACC by bumping the risk-free rate by each delta. Because
    # ke = rf + beta*ERP and kd often keys off rf, a +Δ on rf moves the realized
    # WACC by roughly +Δ. We capture the ACTUAL realized WACC for the row label
    # rather than assuming the bump passes through one-for-one.
    # ----------------------------------------------------------------------- #
    grid1: list[list[float]] = []
    row_wacc_levels: list[float] = []
    for wd in wacc_deltas:
        bumped_macro = replace(macro, risk_free_rate=base_rf + wd)
        row_prices: list[float] = []
        realized_for_row = float("nan")
        for g in growth_levels:
            cell_assumptions = replace(
                assumptions,
                terminal_growth=g,
                terminal_method="gordon",
            )
            price, realized_wacc = _safe_implied_price(
                company, bumped_macro, cell_assumptions, current_price
            )
            row_prices.append(price)
            # Realized WACC is independent of terminal growth, so the first
            # finite read for this row is representative of the whole row.
            if not is_num(realized_for_row) and is_num(realized_wacc):
                realized_for_row = realized_wacc
        # Fallback label if every cell in the row failed: approximate the WACC as
        # the base WACC shifted by the rf delta (best-effort, keeps labels sane).
        row_wacc_levels.append(realized_for_row)
        grid1.append(row_prices)

    # If some rows never produced a realized WACC, back-fill the label by adding
    # the rf delta to the nearest known realized WACC so the axis stays ordered.
    _backfill_wacc_labels(row_wacc_levels, wacc_deltas)

    results.append(
        SensitivityResult(
            title="DCF implied price: WACC vs terminal growth" + title_suffix,
            row_label="WACC",
            col_label="Terminal growth",
            row_values=row_wacc_levels,
            col_values=list(growth_levels),
            grid=grid1,
        )
    )

    # ----------------------------------------------------------------------- #
    # Grid 2 — EBIT margin (rows) x terminal growth (cols)
    #
    # Margin rows are the base latest EBIT margin shifted by each absolute delta.
    # If the base margin can't be derived we still emit the grid (all NaN) so the
    # structure is predictable.
    # ----------------------------------------------------------------------- #
    base_margin = _base_latest_ebit_margin(company)
    # If the caller already pinned a target margin, center the grid on that
    # instead of the raw historical margin (it's what the DCF would actually use).
    explicit_target = getattr(assumptions, "target_ebit_margin", None)
    if is_num(explicit_target):
        base_margin = float(explicit_target)

    margin_levels = [
        (base_margin + d) if is_num(base_margin) else float("nan")
        for d in margin_deltas
    ]

    grid2: list[list[float]] = []
    for m in margin_levels:
        row_prices = []
        for g in growth_levels:
            if not is_num(m):
                row_prices.append(float("nan"))
                continue
            cell_assumptions = replace(
                assumptions,
                target_ebit_margin=m,
                terminal_growth=g,
                terminal_method="gordon",
            )
            price, _ = _safe_implied_price(
                company, macro, cell_assumptions, current_price
            )
            row_prices.append(price)
        grid2.append(row_prices)

    results.append(
        SensitivityResult(
            title="DCF implied price: EBIT margin vs terminal growth" + title_suffix,
            row_label="EBIT margin",
            col_label="Terminal growth",
            row_values=list(margin_levels),
            col_values=list(growth_levels),
            grid=grid2,
        )
    )

    return results


def _backfill_wacc_labels(levels: list[float], deltas: list[float]) -> None:
    """In-place: fill NaN WACC row labels from a neighbour + the delta spread.

    If at least one row produced a realized WACC, infer the others by assuming
    the rf bump passes through one-for-one (the realized base WACC minus its
    delta gives an implied base WACC; add each delta back). Pure cosmetic — only
    affects axis labels, never the price grid.
    """
    if len(levels) != len(deltas):
        return
    # Find any anchor row that has a finite realized WACC.
    anchor_idx = next((i for i, v in enumerate(levels) if is_num(v)), None)
    if anchor_idx is None:
        return  # nothing to anchor on; leave NaNs as-is
    implied_base = levels[anchor_idx] - deltas[anchor_idx]
    for i, v in enumerate(levels):
        if not is_num(v):
            levels[i] = implied_base + deltas[i]


# --------------------------------------------------------------------------- #
#  Public API — football field
# --------------------------------------------------------------------------- #
def _finite_grid_values(grid) -> list[float]:
    """Flatten a 2-D grid into the list of its finite numeric cells."""
    out: list[float] = []
    if not grid:
        return out
    for row in grid:
        if not row:
            continue
        for cell in row:
            if is_num(cell):
                out.append(float(cell))
    return out


def _find_wacc_growth_grid(sensitivities):
    """Return the WACC x growth SensitivityResult from a list, or None."""
    if not sensitivities:
        return None
    for s in sensitivities:
        # Match on the row label we set above; fall back to a title substring.
        row_label = (getattr(s, "row_label", "") or "").lower()
        if "wacc" in row_label:
            return s
    return None


def _band(center: float, pct: float):
    """Return (low, base, high) around ``center`` scaled by (1-pct, 1, 1+pct).

    For a negative center, ``center*(1-pct)`` exceeds ``center*(1+pct)``, so we
    order the edges with min/max to keep the low <= base <= high invariant that
    the exporters rely on (the base is always ``center`` itself).
    """
    lo = center * (1.0 - pct)
    hi = center * (1.0 + pct)
    return min(lo, hi), center, max(lo, hi)


def build_football_field(report) -> list[FootballFieldRow]:
    """Assemble football-field low/base/high bars from a populated report.

    Reads ``report.company`` / ``report.market`` (via company.market),
    ``report.dcf`` / ``.comps`` / ``.ddm`` / ``.fcfe`` (any may be None) and
    ``report.sensitivities``. Only methods whose inputs exist produce a row, so
    the returned list length varies with data availability.

    Conventions:
      * '52-week range'  : low/high from market 52wk lo/hi, base = current_price.
      * 'DCF'            : WACC x growth grid min/max (widened to contain the
                            headline dcf.implied_price, which is the base),
                            else +/-15% around dcf.implied_price.
      * 'EV/EBITDA comps' / 'P/E comps': spread from comps stats applied to the
                            target metric where available, else the comps implied
                            price summary, else skipped.
      * 'DDM' / 'FCFE'   : +/-10% bands around their implied prices.
    """
    rows: list[FootballFieldRow] = []

    company = getattr(report, "company", None)
    market = getattr(company, "market", None) if company is not None else None
    dcf = getattr(report, "dcf", None)
    comps = getattr(report, "comps", None)
    ddm = getattr(report, "ddm", None)
    fcfe = getattr(report, "fcfe", None)
    sensitivities = getattr(report, "sensitivities", None)

    current_price = getattr(report, "current_price", None)
    if not is_num(current_price):
        # Fall back to the live market price if the report didn't carry one.
        mp = getattr(market, "price", None) if market is not None else None
        current_price = float(mp) if is_num(mp) else float("nan")
    else:
        current_price = float(current_price)

    # ----------------------------------------------------------------------- #
    # 52-week range
    # ----------------------------------------------------------------------- #
    if market is not None:
        lo = getattr(market, "fifty_two_week_low", None)
        hi = getattr(market, "fifty_two_week_high", None)
        if is_num(lo) and is_num(hi):
            low, high = float(lo), float(hi)
            if low > high:  # guard against swapped fields
                low, high = high, low
            base = current_price if is_num(current_price) else (low + high) / 2.0
            # Keep the marker inside the bar for a clean render.
            base = min(max(base, low), high)
            rows.append(
                FootballFieldRow(
                    method="52-week range", low=low, base=base, high=high
                )
            )

    # ----------------------------------------------------------------------- #
    # DCF — prefer the WACC x growth sensitivity spread, else +/-15% band
    # ----------------------------------------------------------------------- #
    if dcf is not None:
        dcf_implied = getattr(dcf, "implied_price", None)
        grid_obj = _find_wacc_growth_grid(sensitivities)
        grid_vals = (
            _finite_grid_values(getattr(grid_obj, "grid", None))
            if grid_obj is not None
            else []
        )
        if grid_vals:
            low = min(grid_vals)
            high = max(grid_vals)
            med = median(grid_vals)
            # Center on the median of the grid; if the point estimate is finite,
            # prefer it as the base (it's the engine's headline number). The grid
            # is Gordon-only, so an exit-multiple headline can fall outside it:
            # widen the bar to contain the headline rather than moving the marker.
            base = float(dcf_implied) if is_num(dcf_implied) else float(med)
            low, high = min(low, base), max(high, base)
            rows.append(
                FootballFieldRow(method="DCF", low=low, base=base, high=high)
            )
        elif is_num(dcf_implied):
            low, base, high = _band(float(dcf_implied), 0.15)
            base = min(max(base, low), high)
            rows.append(
                FootballFieldRow(method="DCF", low=low, base=base, high=high)
            )

    # ----------------------------------------------------------------------- #
    # Comps — EV/EBITDA and P/E rows
    # ----------------------------------------------------------------------- #
    if comps is not None:
        rows.extend(_comps_rows(comps, company))

    # ----------------------------------------------------------------------- #
    # DDM / FCFE — +/-10% bands around the implied price
    # ----------------------------------------------------------------------- #
    if ddm is not None:
        ddm_price = getattr(ddm, "implied_price", None)
        if is_num(ddm_price):
            low, base, high = _band(float(ddm_price), 0.10)
            base = min(max(base, low), high)
            rows.append(
                FootballFieldRow(method="DDM", low=low, base=base, high=high)
            )

    if fcfe is not None:
        fcfe_price = getattr(fcfe, "implied_price", None)
        if is_num(fcfe_price):
            low, base, high = _band(float(fcfe_price), 0.10)
            base = min(max(base, low), high)
            rows.append(
                FootballFieldRow(method="FCFE", low=low, base=base, high=high)
            )

    return rows


def _ev_bridge_inputs(company):
    """Pull the EV->equity bridge inputs (ebitda_latest, net_debt, minority,
    preferred, shares) from ``company`` for re-pricing EV multiples.

    Returns a dict with those keys, or ``None`` if the essential inputs
    (positive latest EBITDA and a usable share count) can't be resolved. Net
    debt / minority / preferred default to 0.0 when the balance sheet is absent,
    mirroring ``models.comps``.
    """
    if company is None:
        return None
    fin = getattr(company, "financials", None)
    bs = getattr(company, "balance_sheet", None)
    market = getattr(company, "market", None)

    ebitda = getattr(fin, "ebitda", None) if fin is not None else None
    ebitda_latest = ebitda[-1] if ebitda else None
    if not is_num(ebitda_latest) or float(ebitda_latest) <= 0:
        return None

    # Prefer market shares outstanding, else latest diluted weighted-average.
    shares = getattr(market, "shares_outstanding", None) if market is not None else None
    if not is_num(shares) or float(shares) <= 0:
        diluted = getattr(fin, "diluted_shares", None) if fin is not None else None
        shares = diluted[-1] if diluted else None
    if not is_num(shares) or float(shares) <= 0:
        return None

    net_debt = minority = preferred = 0.0
    if bs is not None:
        net_debt, _ = net_debt_parts(bs)
        mi = getattr(bs, "minority_interest", 0.0)
        minority = float(mi) if is_num(mi) else 0.0
        pe_eq = getattr(bs, "preferred_equity", 0.0)
        preferred = float(pe_eq) if is_num(pe_eq) else 0.0

    return {
        "ebitda_latest": float(ebitda_latest),
        "net_debt": net_debt,
        "minority": minority,
        "preferred": preferred,
        "shares": float(shares),
    }


def _ev_price_at_multiple(mult: float, bridge: dict):
    """Equity price implied by an EV/EBITDA ``mult`` via the EV->equity bridge.

    price = (mult*ebitda - net_debt - minority - preferred) / shares.
    Returns ``None`` if shares are unusable (guards divide-by-zero).
    """
    shares = bridge.get("shares")
    if not is_num(shares) or float(shares) == 0:
        return None
    ev_star = mult * bridge["ebitda_latest"]
    equity_star = ev_star - bridge["net_debt"] - bridge["minority"] - bridge["preferred"]
    return equity_star / float(shares)


def _comps_rows(comps, company=None) -> list[FootballFieldRow]:
    """Build the EV/EBITDA and P/E football-field rows from a CompsResult.

    Strategy per multiple:
      1. Use the implied price from the peer median as the base.
      2. Spread the band by the dispersion of the peer multiple:
           * Equity multiples (P/E, P/B) scale the base price proportionally by
             p25/median and p75/median.
           * EV multiples (EV/EBITDA) re-apply the EV->equity bridge at the p25
             and p75 peer multiples, since proportional scaling of an equity
             price is only valid for equity multiples. If the bridge inputs
             aren't available, this falls back to the symmetric band below.
      3. If the per-multiple stats aren't usable, fall back to the overall
         ``implied_price_summary`` low/median/high.
    """
    out: list[FootballFieldRow] = []

    stats = getattr(comps, "stats", None) or {}
    implied = getattr(comps, "implied", None) or {}
    summary = getattr(comps, "implied_price_summary", None) or {}

    # EV multiples need the EV->equity bridge to re-price at p25/p75; equity
    # multiples (P/E, P/B) use proportional scaling.
    ev_keys = {"ev_ebitda", "ev_sales"}
    bridge = _ev_bridge_inputs(company)

    mapping = [
        ("ev_ebitda", "EV/EBITDA comps"),
        ("pe", "P/E comps"),
    ]

    used_per_multiple = False
    for key, label in mapping:
        base_price = implied.get(key) if isinstance(implied, dict) else None
        if not is_num(base_price):
            continue
        base_price = float(base_price)

        mult_stats = stats.get(key) if isinstance(stats, dict) else None
        low_price = high_price = None
        if isinstance(mult_stats, dict):
            med = mult_stats.get("median")
            p25 = mult_stats.get("p25")
            p75 = mult_stats.get("p75")
            if key in ev_keys and bridge is not None:
                # Re-apply the EV->equity bridge at the p25/p75 peer multiples:
                # price(mult) = (mult*ebitda - net_debt - minority - preferred)/shares.
                if is_num(p25):
                    low_price = _ev_price_at_multiple(float(p25), bridge)
                if is_num(p75):
                    high_price = _ev_price_at_multiple(float(p75), bridge)
            elif is_num(med) and med != 0:
                # Scale the implied price by the dispersion of the peer multiple.
                if is_num(p25):
                    low_price = base_price * (float(p25) / float(med))
                if is_num(p75):
                    high_price = base_price * (float(p75) / float(med))

        if not is_num(low_price) or not is_num(high_price):
            # Fall back to a symmetric +/-10% band around the implied price.
            lo, _, hi = _band(base_price, 0.10)
            low_price = lo if not is_num(low_price) else low_price
            high_price = hi if not is_num(high_price) else high_price

        low_price, high_price = float(low_price), float(high_price)
        if low_price > high_price:
            low_price, high_price = high_price, low_price
        base = min(max(base_price, low_price), high_price)
        out.append(
            FootballFieldRow(
                method=label, low=low_price, base=base, high=high_price
            )
        )
        used_per_multiple = True

    # Fallback: if neither per-multiple implied price was usable but the engine
    # produced an overall summary, emit a single generic "Comps" bar.
    if not used_per_multiple and isinstance(summary, dict):
        lo = summary.get("low")
        med = summary.get("median")
        hi = summary.get("high")
        if is_num(lo) and is_num(hi):
            low_price, high_price = float(lo), float(hi)
            if low_price > high_price:
                low_price, high_price = high_price, low_price
            base = float(med) if is_num(med) else (low_price + high_price) / 2.0
            base = min(max(base, low_price), high_price)
            out.append(
                FootballFieldRow(
                    method="Comps", low=low_price, base=base, high=high_price
                )
            )

    return out
