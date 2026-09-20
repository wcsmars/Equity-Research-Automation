"""Trading-comparables ("comps") valuation.

Given a target company and a set of peer tickers, this module builds a comps
table of trailing multiples, trims outliers per multiple, computes summary
statistics, and derives an implied share price by applying each peer-median
multiple to the target's own metric.

Valuation methods:

  * EV multiples (ev_ebitda, ev_sales) imply an enterprise value, from which the
    non-equity claims (net debt + minority interest + preferred) are stripped to
    get equity value, then divided by shares.
  * Equity multiples (pe, pb) apply directly to per-share earnings / book value.
  * peg is display-only for the target unless a clean earnings-growth figure is
    available to back out an implied P/E.

All monetary inputs are absolute units (not millions); multiples are pure ratios.
The provider is touched ONLY through the DataProvider interface
(`suggest_peers` / `get_peer_comp_rows`). Never crashes on missing data: every
access is guarded and human-readable issues are appended to `CompsResult.notes`.

"""

from __future__ import annotations

from typing import Optional

from .. import config
from ..data.base import DataProvider
from ..schemas import CompanyData, CompRow, CompsResult
from ..utils import (
    cagr,
    is_num,
    median,
    safe_div,
    summary_stats,
    trim_outliers,
)


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def _empty_summary() -> dict:
    """The shape of `implied_price_summary` when nothing usable was produced."""
    return {"low": None, "median": None, "high": None}


def _target_shares(company: CompanyData) -> Optional[float]:
    """Best share count for the target: market shares_outstanding, else latest
    diluted weighted-average shares. Returns None if neither is usable."""
    market = getattr(company, "market", None)
    if market is not None:
        so = getattr(market, "shares_outstanding", None)
        if is_num(so) and so > 0:
            return float(so)
    fin = getattr(company, "financials", None)
    if fin is not None:
        try:
            ds = fin.diluted_shares[-1] if fin.diluted_shares else None
        except (IndexError, AttributeError):
            ds = None
        if is_num(ds) and ds > 0:
            return float(ds)
    return None


def _latest(series: Optional[list]) -> Optional[float]:
    """Most-recent (last) finite element of an oldest->newest series, else None."""
    if not series:
        return None
    try:
        v = series[-1]
    except (IndexError, TypeError):
        return None
    return float(v) if is_num(v) else None


def _net_income_cagr(fin) -> Optional[float]:
    """Net-income CAGR across the available history (oldest->newest).

    Returns None if fewer than two positive endpoints exist (a sign change makes
    growth meaningless for a PEG). The result is a decimal (0.12 == 12%).
    """
    if fin is None:
        return None
    ni = getattr(fin, "net_income", None)
    if not ni or len(ni) < 2:
        return None
    first, last = ni[0], ni[-1]
    periods = len(ni) - 1
    return cagr(first, last, periods)


# --------------------------------------------------------------------------- #
#  Target row construction
# --------------------------------------------------------------------------- #
def _build_target_row(
    company: CompanyData,
    current_price: Optional[float],
    shares: Optional[float],
    net_debt: float,
    minority: float,
    preferred: float,
    notes: list[str],
) -> CompRow:
    """Assemble the target's own CompRow for display.

    enterprise_value = market_cap + net_debt + minority + preferred.
    Trailing multiples are computed from the latest fundamentals; any that cannot
    be derived (missing / non-positive denominator) are left as None.
    """
    market = getattr(company, "market", None)
    fin = getattr(company, "financials", None)

    ticker = getattr(company, "ticker", "") or ""
    name = getattr(company, "name", "") or ticker

    # Market cap: prefer the provider's market cap, else price * shares.
    market_cap: Optional[float] = None
    if market is not None:
        mc = getattr(market, "market_cap", None)
        if is_num(mc) and mc > 0:
            market_cap = float(mc)
    if market_cap is None and is_num(current_price) and shares:
        market_cap = float(current_price) * float(shares)

    # Enterprise value = equity value + net non-equity claims.
    enterprise_value: Optional[float] = None
    if market_cap is not None:
        enterprise_value = market_cap + net_debt + minority + preferred

    # Latest fundamental metrics for the trailing multiples.
    ebitda_latest = _latest(getattr(fin, "ebitda", None)) if fin is not None else None
    revenue_latest = _latest(getattr(fin, "revenue", None)) if fin is not None else None
    ni_latest = _latest(getattr(fin, "net_income", None)) if fin is not None else None
    total_equity = None
    bs = getattr(company, "balance_sheet", None)
    if bs is not None:
        te = getattr(bs, "total_equity", None)
        total_equity = float(te) if is_num(te) else None

    # EV-based multiples (only meaningful for a positive EV and denominator).
    # A net-cash target with EV <= 0 must show None/NM, not a negative multiple.
    ev_ebitda = None
    ev_sales = None
    if enterprise_value is not None and enterprise_value > 0:
        if is_num(ebitda_latest) and ebitda_latest > 0:
            ev_ebitda = safe_div(enterprise_value, ebitda_latest)
        if is_num(revenue_latest) and revenue_latest > 0:
            ev_sales = safe_div(enterprise_value, revenue_latest)

    # Equity multiples.
    pe = None
    if is_num(current_price) and is_num(ni_latest) and ni_latest > 0 and shares:
        eps = safe_div(ni_latest, shares)
        pe = safe_div(current_price, eps)

    pb = None
    if market_cap is not None and is_num(total_equity) and total_equity > 0:
        pb = safe_div(market_cap, total_equity)

    # PEG = P/E divided by the earnings growth expressed in percentage points.
    peg = None
    growth = _net_income_cagr(fin)
    if pe is not None and is_num(growth) and growth > 0:
        peg = safe_div(pe, growth * 100.0)

    if market_cap is None:
        notes.append("Target market cap unavailable; target multiples are limited.")

    return CompRow(
        ticker=ticker,
        name=name,
        market_cap=market_cap,
        enterprise_value=enterprise_value,
        ev_ebitda=ev_ebitda,
        ev_sales=ev_sales,
        pe=pe,
        pb=pb,
        peg=peg,
    )


# --------------------------------------------------------------------------- #
#  Peer collection
# --------------------------------------------------------------------------- #
def _row_has_usable_multiple(row: CompRow) -> bool:
    """True if a peer row carries at least one finite, positive multiple."""
    for m in config.COMPS_MULTIPLES:
        v = getattr(row, m, None)
        if is_num(v) and v > 0:
            return True
    return False


def _collect_peer_rows(
    provider: DataProvider,
    peer_tickers: list[str],
    target_ticker: str,
    notes: list[str],
) -> list[CompRow]:
    """Fetch peer comp rows via the provider, dropping the target and any row with
    no usable multiples. Never raises -- a provider failure yields an empty list
    plus a note."""
    try:
        raw_rows = provider.get_peer_comp_rows(list(peer_tickers))
    except Exception as exc:  # pragma: no cover - defensive against provider bugs
        notes.append(f"Failed to fetch peer comp rows: {exc}")
        return []

    if not raw_rows:
        return []

    target_upper = (target_ticker or "").upper()
    kept: list[CompRow] = []
    for row in raw_rows:
        if row is None:
            continue
        rt = (getattr(row, "ticker", "") or "").upper()
        if rt and rt == target_upper:
            # Exclude the target if the provider returned it among the peers.
            continue
        if not _row_has_usable_multiple(row):
            continue
        kept.append(row)
    return kept


# --------------------------------------------------------------------------- #
#  Implied-price math
# --------------------------------------------------------------------------- #
def _implied_from_multiple(
    multiple: str,
    med: Optional[float],
    *,
    shares: Optional[float],
    net_debt: float,
    minority: float,
    preferred: float,
    ebitda_latest: Optional[float],
    revenue_latest: Optional[float],
    ni_latest: Optional[float],
    total_equity: Optional[float],
    target_peg: Optional[float],
    earnings_growth: Optional[float],
) -> Optional[float]:
    """Apply a peer-median multiple to the target's own metric -> implied price.

    EV multiples back out equity value (EV - net debt - minority - preferred)
    before dividing by shares; equity multiples apply per-share. Returns None when
    any required input is missing / non-positive.
    """
    if med is None or not is_num(med) or med <= 0:
        return None

    if multiple == "ev_ebitda":
        if not (is_num(ebitda_latest) and ebitda_latest > 0 and shares):
            return None
        ev_star = med * ebitda_latest
        equity_star = ev_star - net_debt - minority - preferred
        return safe_div(equity_star, shares)

    if multiple == "ev_sales":
        if not (is_num(revenue_latest) and revenue_latest > 0 and shares):
            return None
        ev_star = med * revenue_latest
        equity_star = ev_star - net_debt - minority - preferred
        return safe_div(equity_star, shares)

    if multiple == "pe":
        if not (is_num(ni_latest) and ni_latest > 0 and shares):
            return None
        eps = safe_div(ni_latest, shares)
        if eps is None:
            return None
        return med * eps

    if multiple == "pb":
        if not (is_num(total_equity) and total_equity > 0 and shares):
            return None
        bvps = safe_div(total_equity, shares)
        if bvps is None:
            return None
        return med * bvps

    if multiple == "peg":
        # Display-only unless a clean earnings-growth figure lets us back out an
        # implied P/E: pe_implied = median_peg * (growth% in points); price = pe*eps.
        if not (is_num(earnings_growth) and earnings_growth > 0):
            return None
        if not (is_num(ni_latest) and ni_latest > 0 and shares):
            return None
        eps = safe_div(ni_latest, shares)
        if eps is None:
            return None
        pe_implied = med * (earnings_growth * 100.0)
        return pe_implied * eps

    return None


# --------------------------------------------------------------------------- #
#  Public entry point
# --------------------------------------------------------------------------- #
def run_comps(
    company: CompanyData,
    provider: DataProvider,
    peers: Optional[list[str]],
    current_price: float,
) -> CompsResult:
    """Run a trading-comps valuation for `company`.

    Parameters
    ----------
    company : CompanyData
        The target. Money fields are absolute units; series oldest->newest.
    provider : DataProvider
        Used ONLY via `suggest_peers` / `get_peer_comp_rows`.
    peers : list[str] | None
        Explicit peer tickers. If None/empty, the provider is asked to suggest
        peers; if that is still empty, an empty CompsResult (with a note) returns.
    current_price : float
        Latest market price per share for the target.

    Returns
    -------
    CompsResult
        Always returned -- never raises on missing data.
    """
    notes: list[str] = []

    ticker = getattr(company, "ticker", "") or ""

    # --- Non-equity claims (used in both target EV and implied EV->equity) ----- #
    bs = getattr(company, "balance_sheet", None)
    net_debt = 0.0
    minority = 0.0
    preferred = 0.0
    if bs is not None:
        try:
            nd = bs.net_debt
            net_debt = float(nd) if is_num(nd) else 0.0
        except Exception:
            net_debt = 0.0
        mi = getattr(bs, "minority_interest", 0.0)
        minority = float(mi) if is_num(mi) else 0.0
        pe_eq = getattr(bs, "preferred_equity", 0.0)
        preferred = float(pe_eq) if is_num(pe_eq) else 0.0
    else:
        notes.append("Balance sheet unavailable; net debt / minority / preferred treated as 0.")

    shares = _target_shares(company)
    if shares is None:
        notes.append("Share count unavailable; implied prices cannot be computed.")

    # --- Build the target's own display row ------------------------------------ #
    target_row = _build_target_row(
        company,
        current_price,
        shares,
        net_debt,
        minority,
        preferred,
        notes,
    )

    # --- Resolve peer tickers --------------------------------------------------- #
    peer_tickers: list[str] = []
    if peers:
        peer_tickers = [t for t in peers if t]
    else:
        try:
            suggested = provider.suggest_peers(ticker)
            peer_tickers = [t for t in (suggested or []) if t]
        except Exception as exc:  # defensive: suggest_peers should never blow up the run
            notes.append(f"Peer suggestion failed: {exc}")
            peer_tickers = []

    # Drop the target itself from the requested set (case-insensitive).
    target_upper = ticker.upper()
    peer_tickers = [t for t in peer_tickers if t.upper() != target_upper]

    if not peer_tickers:
        notes.append("No peer tickers available; comps could not be computed.")
        return CompsResult(
            target=target_row,
            peers=[],
            stats={},
            implied={},
            implied_price_summary=_empty_summary(),
            notes=notes,
        )

    # --- Fetch and clean peer rows --------------------------------------------- #
    peer_rows = _collect_peer_rows(provider, peer_tickers, ticker, notes)
    if not peer_rows:
        notes.append("No usable peers returned by the data provider; comps could not be computed.")
        return CompsResult(
            target=target_row,
            peers=[],
            stats={},
            implied={},
            implied_price_summary=_empty_summary(),
            notes=notes,
        )

    # --- Per-multiple trimming + summary statistics ---------------------------- #
    stats: dict = {}
    medians: dict = {}
    for m in config.COMPS_MULTIPLES:
        raw_vals = [getattr(row, m, None) for row in peer_rows]
        trimmed = trim_outliers(raw_vals, config.COMPS_OUTLIER_FACTOR)
        stats[m] = summary_stats(trimmed)
        medians[m] = median(trimmed)  # peer median used for implied price
        if not trimmed:
            notes.append(f"No usable peer values for {m} after outlier trimming.")

    # --- Target metrics needed for implied prices ------------------------------ #
    fin = getattr(company, "financials", None)
    ebitda_latest = _latest(getattr(fin, "ebitda", None)) if fin is not None else None
    revenue_latest = _latest(getattr(fin, "revenue", None)) if fin is not None else None
    ni_latest = _latest(getattr(fin, "net_income", None)) if fin is not None else None
    total_equity = None
    if bs is not None:
        te = getattr(bs, "total_equity", None)
        total_equity = float(te) if is_num(te) else None
    earnings_growth = _net_income_cagr(fin)

    # --- Implied price per multiple -------------------------------------------- #
    implied: dict = {}
    for m in config.COMPS_MULTIPLES:
        implied[m] = _implied_from_multiple(
            m,
            medians.get(m),
            shares=shares,
            net_debt=net_debt,
            minority=minority,
            preferred=preferred,
            ebitda_latest=ebitda_latest,
            revenue_latest=revenue_latest,
            ni_latest=ni_latest,
            total_equity=total_equity,
            target_peg=getattr(target_row, "peg", None),
            earnings_growth=earnings_growth,
        )

    # --- Flag EV multiples whose bridge yields a non-positive equity value ----- #
    # These are dropped from the summary below; explain why so net-debt-heavy
    # targets are not silently excluded.
    for m in ("ev_ebitda", "ev_sales"):
        p = implied.get(m)
        if is_num(p) and p <= 0:
            notes.append(
                f"{m} implies non-positive equity value (net debt exceeds implied EV); "
                "excluded from summary."
            )

    # --- Summary across all non-None implied prices ---------------------------- #
    implied_prices = [p for p in implied.values() if is_num(p) and p > 0]
    if implied_prices:
        implied_price_summary = {
            "low": min(implied_prices),
            "median": median(implied_prices),
            "high": max(implied_prices),
        }
    else:
        implied_price_summary = _empty_summary()
        notes.append("No implied prices could be derived from the peer medians.")

    return CompsResult(
        target=target_row,
        peers=peer_rows,
        stats=stats,
        implied=implied,
        implied_price_summary=implied_price_summary,
        notes=notes,
    )
