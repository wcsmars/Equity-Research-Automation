"""Live market data + trading-comps multiples via yfinance.

`YFinanceClient` supplies the *market* side of the world (price, market cap, beta,
annual dividend, 52-week range, sector/industry) and the trailing valuation
multiples used by the comps model. It can also, as a fallback, reconstruct a rough
`AnnualFinancials` + `BalanceSheetSnapshot` from yfinance's statement DataFrames
for non-US issuers that SEC EDGAR cannot serve.

Currencies
----------
* Market data is returned in the MAJOR unit of the quote currency: Yahoo quotes
  London, Johannesburg and Tel Aviv listings in pence / cents / agorot (``GBp``,
  ``ZAc``, ``ILA``), which are converted to GBP / ZAR / ILS.
* yfinance statements are in the issuer's reporting currency
  (``info['financialCurrency']``), e.g. TWD for the USD-quoted TSM ADR. The
  fallback converts them into the quote currency at one spot FX rate (a Yahoo
  ``XXXYYY=X`` quote) so price and fundamentals are comparable; if no rate can
  be fetched it leaves them unconverted and adds a prominent WARNING note.
* Data-quality notes ride on the returned objects as a ``_source_notes``
  attribute (MarketData, and the fallback's AnnualFinancials); HybridProvider
  copies them into ``CompanyData.source_notes``.

Design notes
------------
* yfinance is imported LAZILY inside each method so that merely importing this
  module (and the wider package) never requires the dependency. EDGAR-only use
  therefore works without yfinance installed.
* yfinance's `.info` dictionary is notoriously sparse and inconsistent — many keys
  are absent for any given ticker, ETFs, or non-US listings. EVERY access goes
  through `.get(...)` with a default and `_num()` coercion so a missing/`NaN`/
  string field degrades to `None` instead of crashing.
* `get_market_data` is the only method that raises (a `DataError`) — and only when
  a price is genuinely unobtainable, since price underpins the whole valuation.
  Everything else returns `None` / `[]` on failure so peer enrichment never aborts
  a run.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Optional

from ..schemas import (
    AnnualFinancials,
    BalanceSheetSnapshot,
    CompRow,
    MarketData,
)
from ..utils import is_num
from .base import DataError

# Yahoo quote currencies expressed in minor units -> (major ISO code, factor).
_MINOR_UNITS = {
    "GBp": ("GBP", 0.01),
    "GBX": ("GBP", 0.01),
    "ZAc": ("ZAR", 0.01),
    "ILA": ("ILS", 0.01),
}

# sharesOutstanding more than this far from marketCap/price is on a different
# share basis (one class of a multi-class issuer, or ordinary shares vs ADSs).
_SHARE_MISMATCH_TOL = 0.05

# Monetary fields scaled by an FX conversion (share counts and years are not).
_MONEY_SERIES = (
    "revenue", "ebit", "ebitda", "net_income", "dep_amort", "capex",
    "change_in_nwc", "interest_expense", "tax_expense", "pretax_income",
    "dividends_paid",
)
_MONEY_BALANCE = (
    "total_debt", "cash_and_investments", "total_equity", "minority_interest",
    "preferred_equity",
)


# --------------------------------------------------------------------------- #
#  Small coercion helpers (kept local; these are yfinance-specific janitorial
#  chores rather than reusable numeric primitives).
# --------------------------------------------------------------------------- #
def _num(x: object) -> Optional[float]:
    """Coerce a yfinance value to a finite float, or None.

    yfinance frequently returns strings ('Infinity'), NaNs, None, or sentinel
    zeros for missing fields. We accept only genuinely finite real numbers.
    """
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if is_num(v) else None


def _pos(x: object) -> Optional[float]:
    """Like `_num` but also rejects non-positive values.

    Useful for fields where a non-positive number is meaningless (a price, a
    P/E we intend to display, shares outstanding, etc.).
    """
    v = _num(x)
    return v if (v is not None and v > 0) else None


def _str(x: object) -> Optional[str]:
    """Coerce to a non-empty stripped string, or None."""
    if x is None:
        return None
    try:
        s = str(x).strip()
    except Exception:  # pragma: no cover - defensive
        return None
    return s or None


def major_currency(code: Optional[str]) -> tuple[Optional[str], float]:
    """``(major ISO code, factor)`` for a Yahoo currency code.

    ``GBp`` (pence) -> ``('GBP', 0.01)``, ``ZAc`` -> ``('ZAR', 0.01)``,
    ``ILA`` -> ``('ILS', 0.01)``; anything else is already a major unit and is
    returned upper-cased with factor 1.0. The minor codes are case-sensitive
    (``GBp`` is pence, ``GBP`` is pounds).
    """
    s = _str(code)
    if s is None:
        return None, 1.0
    if s in _MINOR_UNITS:
        return _MINOR_UNITS[s]
    return s.upper(), 1.0


def _unit_scale(ratio_if_minor: Optional[float], factor: float) -> float:
    """Multiplier that takes a Yahoo amount to the MAJOR currency unit.

    For a minor-unit quote (``factor`` 0.01) Yahoo is not consistent about
    whether derived fields (market cap, dividend rate) are in the minor or the
    major unit. `ratio_if_minor` compares the amount with the same quantity
    rebuilt from the minor-unit price: ~1 means it is in minor units (scale by
    `factor`), ~`factor` means it is already major (scale 1.0). The closer
    reading wins; without a comparison the amount is taken to be in the quote's
    own (minor) unit.
    """
    if factor == 1.0 or ratio_if_minor is None or not ratio_if_minor > 0:
        return factor
    if abs(math.log(ratio_if_minor / factor)) < abs(math.log(ratio_if_minor)):
        return 1.0
    return factor


def scale_fundamentals(
    financials: AnnualFinancials, balance_sheet: BalanceSheetSnapshot, rate: float
) -> tuple[AnnualFinancials, BalanceSheetSnapshot]:
    """Copies of the fundamentals with every monetary amount multiplied by `rate`.

    Used for FX conversion: all flow series and balance-sheet amounts are
    scaled; fiscal years and share counts are not. Dynamic attributes (e.g.
    ``_source_notes``) are not carried over.
    """
    fin = dataclasses.replace(
        financials,
        **{k: [v * rate for v in getattr(financials, k)] for k in _MONEY_SERIES},
    )
    bs = dataclasses.replace(
        balance_sheet,
        **{k: getattr(balance_sheet, k) * rate for k in _MONEY_BALANCE},
    )
    return fin, bs


class YFinanceClient:
    """Thin, defensive wrapper around yfinance for market data and comps."""

    # ----------------------------------------------------------------- #
    #  Internal: fetch the (.info dict, Ticker object) for a symbol.
    # ----------------------------------------------------------------- #
    def _ticker(self, ticker: str):
        """Return a yfinance Ticker object (lazy import). Raises on import error."""
        import yfinance as yf  # lazy: package imports without yfinance installed

        return yf.Ticker(ticker)

    def _info(self, tk) -> dict:
        """Best-effort `.info` dict for a Ticker; empty dict on any failure.

        `.info` triggers a network call and can throw (HTTP errors, JSON decode
        errors, rate limits) or return None — all of which we swallow here.
        """
        try:
            info = tk.info
        except Exception:
            return {}
        return info if isinstance(info, dict) else {}

    def _fast_value(self, tk, keys: tuple[str, ...], coerce=_pos):
        """First usable `fast_info` field among `keys` (e.g. last_price, shares).

        `fast_info` supports both attribute and mapping access depending on the
        yfinance version, so we try both; `coerce` validates each candidate.
        `fast_info` is a lighter endpoint (price history) than `.info`, so it
        often still works when `.info` is rate-limited. Any failure -> None.
        """
        fi = None
        try:
            fi = tk.fast_info
        except Exception:
            return None
        if fi is None:
            return None
        # Try attribute access first, then mapping-style access.
        for key in keys:
            try:
                val = coerce(getattr(fi, key))
            except Exception:
                val = None
            if val is not None:
                return val
        for key in keys:
            try:
                val = coerce(fi[key])  # type: ignore[index]
            except Exception:
                val = None
            if val is not None:
                return val
        return None

    def _fast_last_price(self, tk) -> Optional[float]:
        """Pull a last price from `fast_info`, or None."""
        return self._fast_value(tk, ("last_price", "lastPrice"))

    def _quote_currency(self, tk, info: dict) -> Optional[str]:
        """Raw Yahoo quote currency: ``info['currency']``, else fast_info's."""
        return _str(info.get("currency")) or self._fast_value(tk, ("currency",), _str)

    # ----------------------------------------------------------------- #
    #  Public: spot FX rate (never raises).
    # ----------------------------------------------------------------- #
    def get_fx_rate(self, from_ccy: str, to_ccy: str) -> Optional[tuple[float, str]]:
        """Spot units of `to_ccy` per one `from_ccy`, plus the quote(s) used.

        Tries Yahoo's direct pair (``TWDUSD=X``), then the inverse pair
        (``1/USDTWD=X``), then a cross through USD. Returns None when no
        positive rate can be obtained. Never raises.
        """
        f, t = (_str(from_ccy) or "").upper(), (_str(to_ccy) or "").upper()
        if not f or not t:
            return None
        if f == t:
            return 1.0, "same currency"
        hit = self._fx_pair(f, t)
        if hit is not None:
            return hit
        if "USD" not in (f, t):
            a, b = self._fx_pair(f, "USD"), self._fx_pair("USD", t)
            if a is not None and b is not None:
                return a[0] * b[0], f"{a[1]} x {b[1]}"
        return None

    def _fx_pair(self, f: str, t: str) -> Optional[tuple[float, str]]:
        """`t` per one `f` from the direct Yahoo pair or its inverse, or None."""
        for sym, invert in ((f"{f}{t}=X", False), (f"{t}{f}=X", True)):
            try:
                tk = self._ticker(sym)
            except Exception:
                continue
            px = self._fast_last_price(tk)
            if px is None:
                info = self._info(tk)
                px = _pos(info.get("regularMarketPrice")) or _pos(info.get("previousClose"))
            if px is not None:
                return (1.0 / px, f"1/{sym}") if invert else (px, sym)
        return None

    # ----------------------------------------------------------------- #
    #  Public: live market data for the target.
    # ----------------------------------------------------------------- #
    def get_market_data(self, ticker: str) -> MarketData:
        """Return live :class:`MarketData` for ``ticker``.

        Field mapping (first non-None wins):
          price              <- info.currentPrice -> fast_info.last_price -> previousClose
          shares_outstanding <- info.sharesOutstanding -> fast_info.shares, replaced by
                                marketCap/price when they differ by >5% (multi-class
                                issuers / ADRs report one class or ordinary shares)
          market_cap         <- info.marketCap (fallback price * shares)
          beta               <- info.beta
          dividend_per_share <- info.dividendRate (indicated annual rate, used as D0)
                                -> trailingAnnualDividendRate
          52wk low/high      <- info.fiftyTwoWeekLow / fiftyTwoWeekHigh
          sector/industry    <- info.sector / industry
          currency           <- info.currency -> fast_info.currency (default 'USD'),
                                minor units (GBp/ZAc/ILA) converted to the major unit
          name               <- info.longName -> shortName -> ticker

        When neither a share count nor a market cap is available both are left
        at 0.0 (the schema types them as float); the hybrid provider backfills
        them from the statements, with a note, or warns. Data-quality notes are
        attached as ``_source_notes`` on the returned object.

        Raises :class:`DataError` if no price can be obtained.
        """
        try:
            tk = self._ticker(ticker)
        except Exception as exc:  # yfinance missing or construction failed
            raise DataError(
                f"Could not initialize yfinance for '{ticker}': {exc}"
            ) from exc

        info = self._info(tk)
        notes: list[str] = []
        if not info:
            notes.append(
                "Yahoo quote summary (.info) unavailable; market data limited to "
                "the price endpoint (no beta, dividend or 52-week range from Yahoo)"
            )

        # --- price: try .info fields, then the lighter fast_info endpoint ---- #
        price = _pos(info.get("currentPrice"))
        if price is None:
            price = self._fast_last_price(tk)
        if price is None:
            price = _pos(info.get("previousClose"))
        if price is None:
            price = _pos(info.get("regularMarketPrice"))
        if price is None:
            raise DataError(
                f"No obtainable price for '{ticker}' (yfinance .info/fast_info "
                "returned no usable price field)."
            )

        # --- currency: convert minor-unit quotes (pence etc.) to major ------ #
        quote_ccy = self._quote_currency(tk, info)
        if quote_ccy is None:
            quote_ccy = "USD"
            notes.append("quote currency unavailable from Yahoo; assumed USD")
        currency, unit = major_currency(quote_ccy)
        if unit != 1.0:
            notes.append(
                f"Yahoo quotes {ticker.upper()} in {quote_ccy} ({currency} minor "
                f"units); price, 52-week range and dividend converted to {currency}"
            )
        price_quote = price  # in the quote's own (possibly minor) unit
        price = price_quote * unit

        # --- shares & market cap (with mutual fallbacks) --------------------- #
        shares = _pos(info.get("sharesOutstanding"))
        mcap_raw = _pos(info.get("marketCap"))
        if shares is None and mcap_raw is None:
            shares = self._fast_value(tk, ("shares",))
        market_cap: Optional[float] = None
        if mcap_raw is not None:
            ratio = mcap_raw / (price_quote * shares) if shares is not None else None
            market_cap = mcap_raw * _unit_scale(ratio, unit)
            implied = market_cap / price
            if shares is None:
                # Back out an implied share count so per-share math works.
                shares = implied
            elif abs(implied / shares - 1.0) > _SHARE_MISMATCH_TOL:
                # Multi-class issuers (GOOGL, BRK-B) report one class's shares
                # and ADRs sometimes ordinary shares, while marketCap covers the
                # whole company on the quoted line's basis.
                notes.append(
                    f"sharesOutstanding ({shares:,.0f}) differs from marketCap/price "
                    f"({implied:,.0f}); using marketCap/price (multi-class or ADR "
                    "share basis)"
                )
                shares = implied
        elif shares is not None:
            market_cap = price * shares
        # The dataclass types these as float: when neither is known leave 0.0;
        # HybridProvider backfills them from the statements (with a note) or
        # adds a WARNING that the market cap is unavailable.
        if shares is None or market_cap is None:
            shares, market_cap = 0.0, 0.0

        # --- dividend per share -------------------------------------------- #
        # Yahoo's dividendRate is the indicated (current annualized) dividend,
        # used as D0 by the DDM; trailingAnnualDividendRate (TTM) only fills in
        # when it is missing, since TTM lags cuts and includes specials.
        dps = _num(info.get("dividendRate"))
        if dps is None:
            dps = _num(info.get("trailingAnnualDividendRate"))
        if dps is not None and unit != 1.0:
            # Disambiguate the dividend's unit with the unitless trailing yield.
            yld = _pos(info.get("trailingAnnualDividendYield"))
            ratio = dps / (yld * price_quote) if yld is not None else None
            dps = dps * _unit_scale(ratio, unit)

        def _px(key: str) -> Optional[float]:
            v = _num(info.get(key))
            return v * unit if v is not None else None

        name = (
            _str(info.get("longName"))
            or _str(info.get("shortName"))
            or ticker.upper()
        )

        md = MarketData(
            ticker=ticker.upper(),
            name=name,
            currency=currency,
            price=price,
            shares_outstanding=shares,
            market_cap=market_cap,
            beta=_num(info.get("beta")),
            dividend_per_share=dps,
            fifty_two_week_low=_px("fiftyTwoWeekLow"),
            fifty_two_week_high=_px("fiftyTwoWeekHigh"),
            sector=_str(info.get("sector")),
            industry=_str(info.get("industry")),
        )
        md._source_notes = notes  # type: ignore[attr-defined]
        return md

    # ----------------------------------------------------------------- #
    #  Public: a single comps row (never raises).
    # ----------------------------------------------------------------- #
    def get_comp_row(self, ticker: str) -> Optional[CompRow]:
        """Build a :class:`CompRow` of trailing multiples from ``.info``.

        Field mapping:
          market_cap       <- marketCap
          enterprise_value <- enterpriseValue
          ev_ebitda        <- enterpriseToEbitda
          ev_sales         <- enterpriseToRevenue
          pe               <- trailingPE
          pb               <- priceToBook
          peg              <- pegRatio (fallback trailingPegRatio)

        Returns ``None`` (never raises) if the ticker can't be resolved or yields
        no usable data at all.
        """
        try:
            tk = self._ticker(ticker)
        except Exception:
            return None

        info = self._info(tk)
        if not info:
            return None

        name = (
            _str(info.get("longName"))
            or _str(info.get("shortName"))
            or ticker.upper()
        )

        # PEG can live under either key depending on yfinance version.
        peg = _num(info.get("pegRatio"))
        if peg is None:
            peg = _num(info.get("trailingPegRatio"))

        row = CompRow(
            ticker=ticker.upper(),
            name=name,
            market_cap=_pos(info.get("marketCap")),
            enterprise_value=_num(info.get("enterpriseValue")),
            ev_ebitda=_num(info.get("enterpriseToEbitda")),
            ev_sales=_num(info.get("enterpriseToRevenue")),
            pe=_num(info.get("trailingPE")),
            pb=_num(info.get("priceToBook")),
            peg=peg,
        )

        # If literally every valuation field is missing, the row is useless as a
        # comp; treat that as an unresolved ticker.
        has_any = any(
            v is not None
            for v in (
                row.ev_ebitda,
                row.ev_sales,
                row.pe,
                row.pb,
                row.peg,
                row.enterprise_value,
                row.market_cap,
            )
        )
        return row if has_any else None

    # ----------------------------------------------------------------- #
    #  Public: many comps rows, skipping failures.
    # ----------------------------------------------------------------- #
    def get_comp_rows(self, tickers: list[str]) -> list[CompRow]:
        """Map :meth:`get_comp_row` over ``tickers``, dropping unresolved ones.

        De-duplicates by upper-cased symbol so the same peer passed twice (or the
        target appearing in its own peer list) yields a single row.
        """
        rows: list[CompRow] = []
        seen: set[str] = set()
        for t in tickers or []:
            sym = _str(t)
            if not sym:
                continue
            key = sym.upper()
            if key in seen:
                continue
            seen.add(key)
            row = self.get_comp_row(sym)
            if row is not None:
                rows.append(row)
        return rows

    # ----------------------------------------------------------------- #
    #  Public: best-effort peer suggestions (yfinance has no robust API).
    # ----------------------------------------------------------------- #
    def suggest_peers(self, ticker: str) -> list[str]:
        """Best-effort peer tickers for ``ticker``: always ``[]``.

        yfinance exposes no peer/screener data: none of the modules its `.info`
        requests (financialData, quoteType, defaultKeyStatistics, assetProfile,
        summaryDetail, the v7 quote) carries a related-tickers field. We never
        fabricate peers from a sector string either, so peers must be passed
        explicitly (README: "Peers are not auto-discovered"). Returning without
        a network call matters because the comps model asks on every
        (cached) recompute. Never raises.
        """
        return []

    # ----------------------------------------------------------------- #
    #  Public: fundamentals fallback from yfinance statement DataFrames.
    # ----------------------------------------------------------------- #
    def get_annual_financials_fallback(
        self, ticker: str
    ) -> Optional[tuple[AnnualFinancials, BalanceSheetSnapshot]]:
        """Reconstruct (AnnualFinancials, BalanceSheetSnapshot) from yfinance.

        Parses ``Ticker(...).financials`` (income statement), ``.cashflow`` and
        ``.balance_sheet`` — pandas DataFrames whose COLUMNS are period-end
        Timestamps ordered NEWEST-first. We reverse them to OLDEST->NEWEST to match
        the package's series convention, and keep only the periods where the
        income statement has both revenue and net income (yfinance often adds a
        sparse oldest column), mirroring the EDGAR year axis. Each line takes,
        per period, the first candidate row with a value; lines still missing
        are zero-filled and noted.

        Statements are in the issuer's reporting currency
        (``info['financialCurrency']``); when that differs from the quote
        currency they are converted at one spot FX rate (see
        :meth:`get_fx_rate`), or left unconverted with a WARNING note if no rate
        is available. Notes ride on the returned financials as
        ``_source_notes``.

        Used by the hybrid provider to backfill non-US issuers that EDGAR cannot
        serve. Returns ``None`` on any failure (missing dep, empty frames, no
        revenue), so the caller can decide how to proceed.
        """
        try:
            import pandas as pd  # noqa: F401  (lazy; only needed on this path)

            tk = self._ticker(ticker)
            income = self._frame(tk, "financials")
            cashflow = self._frame(tk, "cashflow")
            balance = self._frame(tk, "balance_sheet")
        except Exception:
            return None

        # Without an income statement there is nothing to anchor the series on.
        if income is None or income.empty:
            return None

        notes: list[str] = []
        rev_names = ("Total Revenue", "TotalRevenue", "Operating Revenue", "OperatingRevenue")
        ni_names = ("Net Income", "NetIncome", "Net Income Common Stockholders")

        # Period-end columns, oldest -> newest. yfinance gives newest-first.
        cols = list(income.columns)
        try:
            cols = sorted(cols)  # Timestamps sort chronologically -> oldest first
        except Exception:
            cols = list(reversed(cols))  # fall back to a simple reversal

        # Keep periods with revenue, and with net income too where that line
        # exists at all (a sparse oldest column otherwise enters the history
        # as a year of real revenue with zero EBIT, capex, shares, ...).
        with_rev = [c for c in cols if self._cell(income, rev_names, c) is not None]
        both = [c for c in with_rev if self._cell(income, ni_names, c) is not None]
        kept = both or with_rev
        if not kept:
            return None  # no revenue anywhere: nothing to value
        dropped = [c for c in cols if c not in kept]
        if dropped:
            notes.append(
                "yfinance fallback: dropped "
                + ", ".join(self._date_iso(c) for c in dropped)
                + " (period without both revenue and net income)"
            )
        cols = kept

        # Derive fiscal years from the column timestamps (period-end year).
        fiscal_years: list[int] = []
        for c in cols:
            yr = self._year_of(c)
            fiscal_years.append(yr if yr is not None else 0)

        # --- helper to pull a row series aligned to `cols` (oldest->newest) --- #
        def series(
            sources: list[tuple[object, tuple[str, ...]]],
            *,
            positive: bool = False,
            label: str = "",
        ) -> list[float]:
            """Per period, the first candidate row (across `sources`) with a value.

            Missing values -> 0.0 (noted when `label` is given); if `positive`,
            store the absolute magnitude (yfinance reports capex / dividends /
            D&A with varying signs).
            """
            out: list[float] = []
            missing: list[int] = []
            for c, fy in zip(cols, fiscal_years):
                val = None
                for df, names in sources:
                    val = self._cell(df, names, c)
                    if val is not None:
                        break
                if val is None:
                    out.append(0.0)
                    missing.append(fy)
                else:
                    out.append(abs(val) if positive else val)
            if label and missing:
                if len(missing) == len(cols):
                    notes.append(f"yfinance fallback: {label} unavailable; filled with 0.0")
                else:
                    notes.append(
                        f"yfinance fallback: {label} missing for "
                        + ", ".join(f"FY{y}" for y in missing)
                        + "; filled with 0.0"
                    )
            return out

        # --- income-statement driven series --------------------------------- #
        revenue = series([(income, rev_names)])
        # Operating income first: Yahoo's 'EBIT' row is pretax income + interest
        # expense (non-operating items included), so it is only a last resort
        # (e.g. banks with no operating-income line), matching EDGAR's basis.
        ebit = series(
            [(income, ("Operating Income", "OperatingIncome",
                       "Total Operating Income As Reported", "EBIT"))],
            label="EBIT (operating income)",
        )
        net_income = series([(income, ni_names)])
        pretax_income = series(
            [(income, ("Pretax Income", "PretaxIncome", "Income Before Tax"))],
            label="pretax income",
        )
        # Signed: a tax benefit stays negative (the effective-tax median needs it).
        tax_expense = series(
            [(income, ("Tax Provision", "TaxProvision", "Income Tax Expense"))],
            label="tax expense",
        )
        interest_expense = series(
            [(income, ("Interest Expense", "InterestExpense",
                       "Interest Expense Non Operating"))],
            positive=True,
        )

        # D&A: prefer the income statement, then the cash-flow statement.
        dep_amort = series(
            [
                (income, (
                    "Reconciled Depreciation",
                    "Depreciation And Amortization In Income Statement",
                    "Depreciation Amortization Depletion Income Statement",
                )),
                (cashflow, (
                    "Depreciation And Amortization",
                    "DepreciationAndAmortization",
                    "Depreciation Amortization Depletion",
                    "Depreciation",
                )),
            ],
            positive=True,
            label="D&A",
        )

        # EBITDA = EBIT + D&A (per the package convention; never looked up).
        ebitda = [e + d for e, d in zip(ebit, dep_amort)]

        # --- cash-flow driven series ---------------------------------------- #
        capex = series(
            [(cashflow, ("Capital Expenditure", "CapitalExpenditure", "Purchase Of PPE"))],
            positive=True,
            label="capex",
        )
        dividends_paid = series(
            [(cashflow, (
                "Cash Dividends Paid",
                "Common Stock Dividend Paid",
                "CommonStockDividendPaid",
                "Dividends Paid",
            ))],
            positive=True,
        )
        # ΔNWC: yfinance's "Change In Working Capital" is signed as a cash-flow
        # contribution (a NWC *increase* is a cash *use* -> negative). Our schema
        # stores ΔNWC as positive = increase in NWC, so negate the cash-flow sign.
        cf_wc = series([(cashflow, ("Change In Working Capital", "ChangeInWorkingCapital"))])
        change_in_nwc = [-v for v in cf_wc]

        # Diluted shares (weighted average), falling back to basic per period.
        diluted_shares = series(
            [(income, (
                "Diluted Average Shares",
                "DilutedAverageShares",
                "Basic Average Shares",
                "BasicAverageShares",
            ))],
            label="diluted shares",
        )

        financials = AnnualFinancials(
            fiscal_years=fiscal_years,
            revenue=revenue,
            ebit=ebit,
            ebitda=ebitda,
            net_income=net_income,
            dep_amort=dep_amort,
            capex=capex,
            change_in_nwc=change_in_nwc,
            interest_expense=interest_expense,
            tax_expense=tax_expense,
            pretax_income=pretax_income,
            dividends_paid=dividends_paid,
            diluted_shares=diluted_shares,
        )

        balance_sheet = self._build_balance_sheet(balance, notes)

        # Reporting currency -> quote currency (FX notes lead the list).
        fx_notes: list[str] = []
        financials, balance_sheet = self._to_quote_currency(
            tk, financials, balance_sheet, fx_notes
        )
        financials._source_notes = fx_notes + notes  # type: ignore[attr-defined]
        return financials, balance_sheet

    def _to_quote_currency(
        self,
        tk,
        financials: AnnualFinancials,
        balance_sheet: BalanceSheetSnapshot,
        notes: list[str],
    ) -> tuple[AnnualFinancials, BalanceSheetSnapshot]:
        """Convert fallback statements into the quote currency's major unit.

        One spot rate for every year: the DCF, FCFE and comps are linear in the
        monetary inputs, so this equals valuing in the reporting currency and
        converting at spot, and the WACC weights need debt and market cap in the
        same currency. If no rate can be fetched the statements are returned
        unconverted with a WARNING note (currencies are never mixed silently).
        """
        info = self._info(tk)
        fin_ccy, fin_unit = major_currency(info.get("financialCurrency"))
        quote_ccy, _ = major_currency(self._quote_currency(tk, info))
        if fin_ccy is None or quote_ccy is None:
            notes.append(
                "yfinance fallback: reporting or quote currency unavailable from "
                "Yahoo; statements assumed to be in the quote currency"
            )
            return financials, balance_sheet
        if fin_ccy == quote_ccy and fin_unit == 1.0:
            return financials, balance_sheet
        fx = self.get_fx_rate(fin_ccy, quote_ccy)
        if fx is None:
            notes.append(
                f"WARNING: financial statements are in {fin_ccy} but the share "
                f"price is in {quote_ccy}, and no {fin_ccy}->{quote_ccy} exchange "
                "rate could be fetched; statements were NOT converted, so DCF, "
                "FCFE and comps per-share values are not comparable with the price"
            )
            return financials, balance_sheet
        rate, how = fx
        notes.append(
            f"Fundamentals converted from {fin_ccy} to {quote_ccy} at spot "
            f"{rate:.6g} ({how}); every year uses this one rate"
        )
        return scale_fundamentals(financials, balance_sheet, rate * fin_unit)

    # ----------------------------------------------------------------- #
    #  Balance-sheet snapshot assembly (most recent period).
    # ----------------------------------------------------------------- #
    def _build_balance_sheet(
        self, balance, notes: Optional[list[str]] = None
    ) -> BalanceSheetSnapshot:
        """Build a :class:`BalanceSheetSnapshot` from the newest balance-sheet column.

        Always returns a snapshot (zero-filled if data is missing, with a note
        in `notes`) so the caller gets a usable object.
        """
        notes = notes if notes is not None else []
        if balance is None or getattr(balance, "empty", True):
            notes.append("yfinance fallback: balance sheet unavailable; debt, cash and equity set to 0.0")
            return BalanceSheetSnapshot(
                as_of="",
                total_debt=0.0,
                cash_and_investments=0.0,
                total_equity=0.0,
            )

        # Newest period-end column.
        try:
            col = max(balance.columns)
        except Exception:
            col = balance.columns[0]
        as_of = self._date_iso(col)

        def val(names: tuple[str, ...]) -> Optional[float]:
            return self._cell(balance, names, col)

        # Total debt: prefer an explicit total, else sum LT + current debt.
        total_debt = val(("Total Debt", "TotalDebt"))
        if total_debt is None:
            lt = val(("Long Term Debt And Capital Lease Obligation", "Long Term Debt", "LongTermDebt"))
            cur = val((
                "Current Debt And Capital Lease Obligation", "Current Debt",
                "CurrentDebt", "Short Term Debt", "ShortTermDebt",
            ))
            if lt is None and cur is None:
                notes.append("yfinance fallback: total debt unavailable; set to 0.0")
            total_debt = (lt or 0.0) + (cur or 0.0)

        # Cash + short-term investments. The combined line already includes the
        # short-term investments, so use it alone; otherwise add the parts.
        combined = val(("Cash Cash Equivalents And Short Term Investments",
                        "CashCashEquivalentsAndShortTermInvestments"))
        if combined is not None:
            cash_and_investments = combined
        else:
            cash = val(("Cash And Cash Equivalents", "CashAndCashEquivalents"))
            sti = val(("Other Short Term Investments", "Short Term Investments"))
            if cash is None and sti is None:
                notes.append("yfinance fallback: cash & equivalents unavailable; set to 0.0")
            cash_and_investments = (cash or 0.0) + (sti or 0.0)

        minority = val(("Minority Interest", "MinorityInterest")) or 0.0
        preferred = val(("Preferred Stock", "PreferredStock", "Preferred Securities Outside Stock Equity")) or 0.0

        # Book equity attributable to the parent (as on EDGAR). The gross line
        # includes noncontrolling interests, so strip them if it is all we have.
        total_equity = val(("Stockholders Equity", "StockholdersEquity", "Common Stock Equity"))
        if total_equity is None:
            gross = val(("Total Equity Gross Minority Interest",))
            if gross is not None:
                total_equity = gross - minority
            else:
                notes.append("yfinance fallback: total equity unavailable; set to 0.0")

        return BalanceSheetSnapshot(
            as_of=as_of,
            total_debt=float(total_debt),
            cash_and_investments=float(cash_and_investments),
            total_equity=float(total_equity or 0.0),
            minority_interest=float(minority),
            preferred_equity=float(preferred),
        )

    # ----------------------------------------------------------------- #
    #  DataFrame access helpers.
    # ----------------------------------------------------------------- #
    def _frame(self, tk, attr: str):
        """Return a statement DataFrame for `attr`, or None on any failure."""
        try:
            df = getattr(tk, attr)
        except Exception:
            return None
        # Guard: yfinance can return None or a non-DataFrame on errors.
        if df is None:
            return None
        try:
            if df.empty:
                return None
        except Exception:
            return None
        return df

    def _row(self, df, names: tuple[str, ...]):
        """Return the first DataFrame row (a Series) whose index label matches.

        Matching is case/whitespace-insensitive against the requested `names`.
        Returns None if no candidate label is present.
        """
        if df is None:
            return None
        try:
            index_labels = list(df.index)
        except Exception:
            return None
        # Build a normalized lookup once.
        norm = {self._norm(lbl): lbl for lbl in index_labels}
        for name in names:
            key = self._norm(name)
            if key in norm:
                try:
                    return df.loc[norm[key]]
                except Exception:
                    return None
        return None

    def _cell(self, df, names: tuple[str, ...], col) -> Optional[float]:
        """Value at period `col` from the first candidate row that has one.

        Unlike taking the first row that merely exists, this coalesces per
        period, so a preferred label that is present but NaN for a period falls
        through to the next candidate instead of becoming 0.0.
        """
        for name in names:
            row = self._row(df, (name,))
            if row is None or not hasattr(row, "get"):
                continue
            try:
                v = _num(row.get(col))
            except Exception:
                v = None
            if v is not None:
                return v
        return None

    @staticmethod
    def _norm(label: object) -> str:
        """Normalize a row label for tolerant matching."""
        return "".join(str(label).lower().split())

    @staticmethod
    def _year_of(col: object) -> Optional[int]:
        """Fiscal-year label from a period-end column label.

        The calendar year of the period end, except that a 52/53-week year
        ending in the first two weeks of January takes the prior year (the same
        rule as the EDGAR parser), so it does not share a label with the next
        fiscal year ending in late December.
        """
        # pandas Timestamp / datetime expose `.year`.
        yr = getattr(col, "year", None)
        if isinstance(yr, int):
            month, day = getattr(col, "month", 0), getattr(col, "day", 0)
            return yr - 1 if (month == 1 and isinstance(day, int) and day <= 14) else yr
        # Fallback: parse a leading 4-digit year from the string form.
        s = str(col)
        if len(s) >= 10 and s[:4].isdigit() and s[5:7] == "01" and s[8:10].isdigit():
            return int(s[:4]) - 1 if int(s[8:10]) <= 14 else int(s[:4])
        if len(s) >= 4 and s[:4].isdigit():
            return int(s[:4])
        return None

    @staticmethod
    def _date_iso(col: object) -> str:
        """Render a period-end column label as an ISO date string (best effort)."""
        try:
            # pandas Timestamp / datetime -> 'YYYY-MM-DD'
            return col.date().isoformat()  # type: ignore[attr-defined]
        except Exception:
            pass
        s = str(col)
        return s[:10] if len(s) >= 10 else s
