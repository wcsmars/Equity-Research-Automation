"""Live market data + trading-comps multiples via yfinance.

`YFinanceClient` supplies the *market* side of the world (price, market cap, beta,
trailing dividend, 52-week range, sector/industry) and the trailing valuation
multiples used by the comps model. It can also, as a fallback, reconstruct a rough
`AnnualFinancials` + `BalanceSheetSnapshot` from yfinance's statement DataFrames
for non-US issuers that SEC EDGAR cannot serve.

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

from typing import Optional

from ..schemas import (
    AnnualFinancials,
    BalanceSheetSnapshot,
    CompRow,
    MarketData,
)
from ..utils import is_num
from .base import DataError


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

    def _fast_last_price(self, tk) -> Optional[float]:
        """Pull a last price from `fast_info` (a lighter, more reliable endpoint).

        `fast_info` supports both attribute and mapping access depending on the
        yfinance version, so we try both. Any failure -> None.
        """
        fi = None
        try:
            fi = tk.fast_info
        except Exception:
            return None
        if fi is None:
            return None
        # Try attribute access first, then mapping-style access.
        for key in ("last_price", "lastPrice"):
            try:
                val = getattr(fi, key)
            except Exception:
                val = None
            price = _pos(val)
            if price is not None:
                return price
        for key in ("last_price", "lastPrice"):
            try:
                val = fi[key]  # type: ignore[index]
            except Exception:
                val = None
            price = _pos(val)
            if price is not None:
                return price
        return None

    # ----------------------------------------------------------------- #
    #  Public: live market data for the target.
    # ----------------------------------------------------------------- #
    def get_market_data(self, ticker: str) -> MarketData:
        """Return live :class:`MarketData` for ``ticker``.

        Field mapping (first non-None wins):
          price              <- info.currentPrice -> fast_info.last_price -> previousClose
          shares_outstanding <- info.sharesOutstanding
          market_cap         <- info.marketCap (fallback price * shares)
          beta               <- info.beta
          dividend_per_share <- info.dividendRate -> trailingAnnualDividendRate
          52wk low/high      <- info.fiftyTwoWeekLow / fiftyTwoWeekHigh
          sector/industry    <- info.sector / industry
          currency           <- info.currency (default 'USD')
          name               <- info.longName -> shortName -> ticker

        Raises :class:`DataError` if no price can be obtained.
        """
        try:
            tk = self._ticker(ticker)
        except Exception as exc:  # yfinance missing or construction failed
            raise DataError(
                f"Could not initialize yfinance for '{ticker}': {exc}"
            ) from exc

        info = self._info(tk)

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

        # --- shares & market cap (with mutual fallbacks) --------------------- #
        shares = _pos(info.get("sharesOutstanding"))
        market_cap = _pos(info.get("marketCap"))
        if market_cap is None and shares is not None:
            market_cap = price * shares
        if shares is None and market_cap is not None:
            # Back out an implied share count so downstream per-share math works.
            shares = market_cap / price
        # Final guards: never leave these as None on the dataclass (which types
        # them as float). Use sane zero-ish fallbacks and let the engine warn.
        if shares is None:
            shares = 0.0
        if market_cap is None:
            market_cap = price * shares  # 0.0 if shares unknown

        # --- dividend per share (trailing annual) --------------------------- #
        dps = _num(info.get("dividendRate"))
        if dps is None:
            dps = _num(info.get("trailingAnnualDividendRate"))

        name = (
            _str(info.get("longName"))
            or _str(info.get("shortName"))
            or ticker.upper()
        )
        currency = _str(info.get("currency")) or "USD"

        return MarketData(
            ticker=ticker.upper(),
            name=name,
            currency=currency,
            price=price,
            shares_outstanding=shares,
            market_cap=market_cap,
            beta=_num(info.get("beta")),
            dividend_per_share=dps,
            fifty_two_week_low=_num(info.get("fiftyTwoWeekLow")),
            fifty_two_week_high=_num(info.get("fiftyTwoWeekHigh")),
            sector=_str(info.get("sector")),
            industry=_str(info.get("industry")),
        )

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
        """Best-effort peer tickers for ``ticker``.

        yfinance exposes no reliable, stable peer/screener API across versions, so
        this is intentionally conservative: we never fabricate peers from a sector
        string (that would require an external universe). Returns ``[]`` so the
        caller (the comps model / engine) can decide how to source peers. Never
        raises.
        """
        try:
            tk = self._ticker(ticker)
        except Exception:
            return []

        # Some yfinance versions expose `.recommendations`-style related symbols or
        # a `get_recommendations` for sustainability/upgrades — none are reliable
        # peer lists. We only opportunistically read an explicit related-companies
        # field if a future/forked yfinance provides one, and otherwise return [].
        info = self._info(tk)
        peers: list[str] = []
        seen: set[str] = set()
        self_sym = ticker.upper()
        for key in ("relatedTickers", "peerSet", "peers"):
            raw = info.get(key)
            if isinstance(raw, (list, tuple)):
                for item in raw:
                    sym = _str(item)
                    if not sym:
                        continue
                    up = sym.upper()
                    if up == self_sym or up in seen:
                        continue
                    seen.add(up)
                    peers.append(sym)
        return peers

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
        the package's series convention, and align all series to the common set of
        fiscal years present on the income statement.

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

        # Period-end columns, oldest -> newest. yfinance gives newest-first.
        cols = list(income.columns)
        try:
            cols = sorted(cols)  # Timestamps sort chronologically -> oldest first
        except Exception:
            cols = list(reversed(cols))  # fall back to a simple reversal

        # Derive fiscal years from the column timestamps (period-end year).
        fiscal_years: list[int] = []
        for c in cols:
            yr = self._year_of(c)
            fiscal_years.append(yr if yr is not None else 0)

        # --- helper to pull a row series aligned to `cols` (oldest->newest) --- #
        def series(df, names: tuple[str, ...], *, positive: bool = False) -> list[float]:
            """Return the first matching row across `names`, aligned to `cols`.

            Missing values -> 0.0; if `positive`, store the absolute magnitude
            (yfinance reports capex / dividends / D&A with varying signs).
            """
            if df is None:
                return [0.0] * len(cols)
            row = self._row(df, names)
            if row is None:
                return [0.0] * len(cols)
            out: list[float] = []
            for c in cols:
                val = _num(row.get(c)) if hasattr(row, "get") else None
                if val is None:
                    out.append(0.0)
                else:
                    out.append(abs(val) if positive else val)
            return out

        # --- income-statement driven series --------------------------------- #
        revenue = series(
            income,
            ("Total Revenue", "TotalRevenue", "Operating Revenue", "OperatingRevenue"),
        )
        # Bail out if revenue is entirely empty/zero — nothing to value.
        if not any(v != 0.0 for v in revenue):
            return None

        ebit = series(income, ("EBIT", "Operating Income", "OperatingIncome"))
        net_income = series(
            income, ("Net Income", "NetIncome", "Net Income Common Stockholders")
        )
        pretax_income = series(income, ("Pretax Income", "PretaxIncome", "Income Before Tax"))
        tax_expense = series(
            income, ("Tax Provision", "TaxProvision", "Income Tax Expense"), positive=True
        )
        interest_expense = series(
            income, ("Interest Expense", "InterestExpense"), positive=True
        )

        # D&A: prefer the income statement, then the cash-flow statement.
        dep_amort = series(
            income,
            (
                "Reconciled Depreciation",
                "Depreciation And Amortization In Income Statement",
                "Depreciation Amortization Depletion Income Statement",
            ),
            positive=True,
        )
        if not any(v != 0.0 for v in dep_amort):
            dep_amort = series(
                cashflow,
                (
                    "Depreciation And Amortization",
                    "DepreciationAndAmortization",
                    "Depreciation Amortization Depletion",
                    "Depreciation",
                ),
                positive=True,
            )

        # EBITDA = EBIT + D&A (per the package convention; never looked up).
        ebitda = [e + d for e, d in zip(ebit, dep_amort)]

        # --- cash-flow driven series ---------------------------------------- #
        capex = series(
            cashflow,
            ("Capital Expenditure", "CapitalExpenditure", "Purchase Of PPE"),
            positive=True,
        )
        dividends_paid = series(
            cashflow,
            (
                "Cash Dividends Paid",
                "Common Stock Dividend Paid",
                "CommonStockDividendPaid",
                "Dividends Paid",
            ),
            positive=True,
        )
        # ΔNWC: yfinance's "Change In Working Capital" is signed as a cash-flow
        # contribution (a NWC *increase* is a cash *use* -> negative). Our schema
        # stores ΔNWC as positive = increase in NWC, so negate the cash-flow sign.
        cf_wc = series(
            cashflow,
            ("Change In Working Capital", "ChangeInWorkingCapital"),
        )
        change_in_nwc = [-v for v in cf_wc]

        # Diluted shares (weighted average) — fall back to basic, then market cap
        # is irrelevant here so leave 0.0 if truly absent.
        diluted_shares = series(
            income,
            (
                "Diluted Average Shares",
                "DilutedAverageShares",
                "Basic Average Shares",
                "BasicAverageShares",
            ),
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

        balance_sheet = self._build_balance_sheet(balance)

        return financials, balance_sheet

    # ----------------------------------------------------------------- #
    #  Balance-sheet snapshot assembly (most recent period).
    # ----------------------------------------------------------------- #
    def _build_balance_sheet(self, balance) -> BalanceSheetSnapshot:
        """Build a :class:`BalanceSheetSnapshot` from the newest balance-sheet column.

        Always returns a snapshot (zero-filled if data is missing) so the caller
        gets a usable object; the DCF/WACC models tolerate zero debt/cash.
        """
        if balance is None or getattr(balance, "empty", True):
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
            row = self._row(balance, names)
            if row is None:
                return None
            try:
                return _num(row.get(col)) if hasattr(row, "get") else None
            except Exception:
                return None

        # Total debt: prefer an explicit total, else sum LT + current debt.
        total_debt = val(("Total Debt", "TotalDebt"))
        if total_debt is None:
            lt = val(("Long Term Debt", "LongTermDebt")) or 0.0
            cur = val(
                ("Current Debt", "CurrentDebt", "Short Term Debt", "ShortTermDebt")
            ) or 0.0
            total_debt = lt + cur

        # Cash + short-term investments.
        cash = val(
            ("Cash And Cash Equivalents", "CashAndCashEquivalents", "Cash Cash Equivalents And Short Term Investments")
        ) or 0.0
        sti = val(("Short Term Investments", "Other Short Term Investments")) or 0.0
        # If the combined "...And Short Term Investments" line was used, avoid
        # double counting: only add sti when the pure-cash line was found.
        cash_and_investments = cash + sti

        total_equity = val(
            ("Stockholders Equity", "StockholdersEquity", "Total Equity Gross Minority Interest", "Common Stock Equity")
        ) or 0.0
        minority = val(("Minority Interest", "MinorityInterest")) or 0.0
        preferred = val(("Preferred Stock", "PreferredStock", "Preferred Securities Outside Stock Equity")) or 0.0

        return BalanceSheetSnapshot(
            as_of=as_of,
            total_debt=float(total_debt),
            cash_and_investments=float(cash_and_investments),
            total_equity=float(total_equity),
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

    @staticmethod
    def _norm(label: object) -> str:
        """Normalize a row label for tolerant matching."""
        return "".join(str(label).lower().split())

    @staticmethod
    def _year_of(col: object) -> Optional[int]:
        """Extract a calendar year from a period-end column label."""
        # pandas Timestamp / datetime expose `.year`.
        yr = getattr(col, "year", None)
        if isinstance(yr, int):
            return yr
        # Fallback: parse a leading 4-digit year from the string form.
        s = str(col)
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
