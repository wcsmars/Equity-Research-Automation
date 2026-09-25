"""Hybrid data provider: EDGAR fundamentals + yfinance market data.

This is the concrete ``DataProvider`` the engine instantiates by default. It
combines two specialized clients:

  * ``EdgarClient`` (``.edgar``) -- authoritative US-GAAP fundamentals straight
    from the SEC's XBRL JSON API. Works for US filers (10-K / 20-F).
  * ``YFinanceClient`` (``.market``) -- live market data (price, shares, beta,
    sector) plus trading-comp multiples, and a best-effort fundamentals fallback
    built from yfinance financial statements for tickers EDGAR can't serve
    (e.g. non-US companies).

Data selection:
  1. Always pull market data from yfinance first.
  2. Try EDGAR for fundamentals (AnnualFinancials + BalanceSheetSnapshot + CIK).
  3. If EDGAR fails for any reason (ticker not in the SEC map, non-US filer,
     network/parse error), fall back to the yfinance fundamentals builder with
     ``cik = None``.
  4. Put fundamentals and price in one currency: EDGAR statements are USD, the
     yfinance fallback converts to the quote currency itself; a missing FX rate
     yields a WARNING note rather than silently mixed currencies.
  5. Backfill market fields yfinance could not supply (shares outstanding,
     market cap, dividend per share) from the statements, with a note each.
  6. Assemble and return ``CompanyData``. ``source_notes`` records which
     fundamentals source was used plus every data-quality note from the
     clients (EDGAR/yfinance gaps, derivations, FX conversion, backfills), so
     the engine can surface them as report warnings.
  7. Raise ``DataError`` only if *neither* source yields usable financials.

The clients are constructed once in ``__init__`` (injectable for testing). We
never crash on a missing field -- every failure path degrades into either the
fallback source or a clear ``DataError``.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

from ..schemas import AnnualFinancials, BalanceSheetSnapshot, CompanyData, CompRow, MarketData
from ..utils import is_num
from .base import DataError, DataProvider
from .edgar import EdgarClient
from .market import YFinanceClient, major_currency, scale_fundamentals


def _notes_of(obj: object) -> list[str]:
    """The ``_source_notes`` a client attached to `obj` (tolerates stubs)."""
    notes = getattr(obj, "_source_notes", None)
    return [str(n) for n in notes] if isinstance(notes, list) else []


def _latest_positive(values: list, years: list) -> tuple[Optional[float], Optional[int]]:
    """Newest positive finite value of an oldest->newest series, with its year."""
    for i in range(len(values) - 1, -1, -1):
        v = values[i]
        if is_num(v) and v > 0:
            return float(v), (years[i] if i < len(years) else None)
    return None, None


class HybridProvider(DataProvider):
    """Combine SEC EDGAR fundamentals with yfinance market data/comps."""

    def __init__(
        self,
        edgar: Optional[EdgarClient] = None,
        market: Optional[YFinanceClient] = None,
    ) -> None:
        # Construct the underlying clients once. Allow injection so tests (and the
        # engine, if it wants custom config) can swap in stubs/preconfigured
        # instances. We do NOT make live calls here -- only on demand.
        self.edgar: EdgarClient = edgar if edgar is not None else EdgarClient()
        self.market: YFinanceClient = market if market is not None else YFinanceClient()

    # ------------------------------------------------------------------ #
    #  Primary entry point: full CompanyData assembly
    # ------------------------------------------------------------------ #
    def get_company_data(self, ticker: str) -> CompanyData:
        """Return fully-populated ``CompanyData`` for ``ticker``.

        Market data is mandatory (we need a live price for every downstream
        valuation). Fundamentals are sourced from EDGAR when possible, otherwise
        from the yfinance fallback. ``DataError`` is raised only when no usable
        fundamentals can be obtained from either source.
        """
        symbol = (ticker or "").strip().upper()
        source_notes: list[str] = []

        # --- 1) Market data (required). Let DataError propagate; without a price
        #         the company cannot be valued. ----------------------------------
        market: MarketData = self.market.get_market_data(symbol)

        # --- 2) Fundamentals: try EDGAR first. ------------------------------------
        financials = None
        balance_sheet = None
        cik: Optional[str] = None
        edgar_name: Optional[str] = None

        from_fallback = False
        try:
            financials, balance_sheet, cik, edgar_name = (
                self.edgar.get_annual_financials(symbol)
            )
            source_notes.append(
                f"Fundamentals: SEC EDGAR (CIK {cik})"
            )
        except DataError as exc:
            # Expected, well-understood failure (e.g. ticker not in SEC map / no
            # XBRL facts). Record it and fall through to the fallback.
            source_notes.append(f"EDGAR unavailable: {exc}")
            financials = balance_sheet = None
            cik = edgar_name = None
        except Exception as exc:  # noqa: BLE001 -- never let a provider bug crash us
            # Any other EDGAR error (network hiccup, unexpected JSON shape, etc.).
            source_notes.append(f"EDGAR error: {exc}")
            financials = balance_sheet = None
            cik = edgar_name = None

        # --- 3) Fall back to yfinance-built fundamentals if EDGAR gave us nothing.
        if financials is None or balance_sheet is None:
            fallback = None
            try:
                fallback = self.market.get_annual_financials_fallback(symbol)
            except Exception as exc:  # noqa: BLE001
                # The fallback builder is best-effort and should return None on
                # failure, but guard against it raising anyway.
                source_notes.append(f"yfinance fallback error: {exc}")
                fallback = None

            if fallback is not None:
                financials, balance_sheet = fallback
                cik = None  # yfinance fundamentals have no CIK
                from_fallback = True
                source_notes.append("Fundamentals: yfinance fallback")

        # --- 4) If both sources failed, we cannot value the company. -------------
        if financials is None or balance_sheet is None:
            raise DataError(
                f"No usable fundamentals for {symbol!r} from SEC EDGAR or yfinance."
            )

        # --- 5) Data-quality notes, currency alignment and market backfills. ------
        # Read the clients' notes before any dataclass copy drops them.
        fundamentals_notes = _notes_of(financials)
        extra = _notes_of(market)
        if not from_fallback:
            financials, balance_sheet = self._edgar_to_quote_currency(
                financials, balance_sheet, market, extra
            )
        market = self._backfill_market(market, financials, from_fallback, extra)
        extra.extend(fundamentals_notes)
        # WARNING notes (unconverted currencies, no market cap) lead the list.
        source_notes.extend(n for n in extra if n.startswith("WARNING"))
        source_notes.extend(n for n in extra if not n.startswith("WARNING"))

        # --- 6) Resolve the display name. Prefer EDGAR's registered name, then
        #         the market name, then the ticker as a last resort. --------------
        name = edgar_name or getattr(market, "name", None) or symbol

        return CompanyData(
            ticker=symbol,
            name=name,
            cik=cik,
            financials=financials,
            balance_sheet=balance_sheet,
            market=market,
            source_notes=source_notes,
        )

    # ------------------------------------------------------------------ #
    #  Helpers: currency alignment and market-data backfill
    # ------------------------------------------------------------------ #
    def _edgar_to_quote_currency(
        self,
        financials: AnnualFinancials,
        balance_sheet: BalanceSheetSnapshot,
        market: MarketData,
        notes: list[str],
    ) -> tuple[AnnualFinancials, BalanceSheetSnapshot]:
        """Convert EDGAR's USD statements when the stock is quoted in another
        currency (rare: EDGAR tickers normally quote in USD)."""
        quote_ccy, _ = major_currency(getattr(market, "currency", None))
        if quote_ccy is None or quote_ccy == "USD":
            return financials, balance_sheet
        fx = None
        get_rate = getattr(self.market, "get_fx_rate", None)
        if callable(get_rate):
            try:
                fx = get_rate("USD", quote_ccy)
            except Exception:  # noqa: BLE001 -- best-effort
                fx = None
        if not (isinstance(fx, tuple) and len(fx) == 2 and is_num(fx[0]) and fx[0] > 0):
            notes.append(
                f"WARNING: EDGAR statements are in USD but the share price is in "
                f"{quote_ccy}, and no USD->{quote_ccy} exchange rate could be "
                "fetched; statements were NOT converted, so DCF, FCFE and comps "
                "per-share values are not comparable with the price"
            )
            return financials, balance_sheet
        rate, how = fx
        notes.append(
            f"Fundamentals converted from USD to {quote_ccy} at spot {rate:.6g} "
            f"({how}); every year uses this one rate"
        )
        return scale_fundamentals(financials, balance_sheet, float(rate))

    @staticmethod
    def _backfill_market(
        market: MarketData,
        financials: AnnualFinancials,
        from_fallback: bool,
        notes: list[str],
    ) -> MarketData:
        """Fill shares / market cap / DPS that yfinance could not supply.

        A missing (0.0) market cap would otherwise enter the WACC as a zero
        equity weight, and a missing DPS would skip the DDM for a dividend
        payer. Only a missing DPS (None) is filled; a reported 0.0 is kept.
        Returns a copy (dataclasses.replace); the caller's MarketData is not
        mutated.
        """
        source = "yfinance" if from_fallback else "EDGAR"
        years = list(getattr(financials, "fiscal_years", None) or [])
        changes: dict = {}
        price = getattr(market, "price", None)

        shares = getattr(market, "shares_outstanding", None)
        if not (is_num(shares) and shares > 0):
            shares, fy = _latest_positive(
                list(getattr(financials, "diluted_shares", None) or []), years
            )
            if shares is not None:
                changes["shares_outstanding"] = shares
                note = (
                    f"shares outstanding unavailable from Yahoo; using FY{fy} "
                    f"diluted weighted-average shares from {source} ({shares:,.0f})"
                )
                if from_fallback:
                    note += "; for an ADR this counts ordinary shares, not ADSs"
                notes.append(note)

        mcap = getattr(market, "market_cap", None)
        if not (is_num(mcap) and mcap > 0):
            if shares is not None and is_num(price) and price > 0:
                changes["market_cap"] = price * shares
                notes.append(
                    f"market cap unavailable from Yahoo; set to price x shares "
                    f"({price * shares:,.0f})"
                )
            else:
                notes.append(
                    "WARNING: market cap unavailable (no share count from Yahoo or "
                    f"{source}); the WACC equity weight and market multiples are "
                    "unreliable"
                )

        if getattr(market, "dividend_per_share", None) is None and shares is not None:
            div, fy = _latest_positive(
                list(getattr(financials, "dividends_paid", None) or [])[-1:], years[-1:]
            )
            if div is not None:
                changes["dividend_per_share"] = div / shares
                notes.append(
                    f"dividend per share unavailable from Yahoo; derived from FY{fy} "
                    f"dividends paid / shares ({div / shares:.4f})"
                )

        if not changes:
            return market
        try:
            return dataclasses.replace(market, **changes)
        except TypeError:  # not a dataclass (e.g. a test stub): set in place
            for k, v in changes.items():
                setattr(market, k, v)
            return market

    # ------------------------------------------------------------------ #
    #  Thin delegations to the market client
    # ------------------------------------------------------------------ #
    def get_market_data(self, ticker: str) -> MarketData:
        """Delegate live market-data lookup to the yfinance client."""
        return self.market.get_market_data(ticker)

    def get_peer_comp_rows(self, tickers: list[str]) -> list[CompRow]:
        """Delegate trading-comps row construction to the yfinance client.

        Unresolvable tickers are skipped (not raised on) by ``get_comp_rows``.
        """
        if not tickers:
            return []
        return self.market.get_comp_rows(tickers)

    def suggest_peers(self, ticker: str) -> list[str]:
        """Delegate best-effort peer suggestion to the yfinance client."""
        try:
            return self.market.suggest_peers(ticker)
        except Exception:  # noqa: BLE001 -- peers are optional; never crash.
            return []
