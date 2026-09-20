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
  4. Assemble and return ``CompanyData``, recording which fundamentals source was
     used in ``source_notes``.
  5. Raise ``DataError`` only if *neither* source yields usable financials.

The clients are constructed once in ``__init__`` (injectable for testing). We
never crash on a missing field -- every failure path degrades into either the
fallback source or a clear ``DataError``.
"""

from __future__ import annotations

from typing import Optional

from ..schemas import CompanyData, CompRow, MarketData
from .base import DataError, DataProvider
from .edgar import EdgarClient
from .market import YFinanceClient


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
                source_notes.append("Fundamentals: yfinance fallback")

        # --- 4) If both sources failed, we cannot value the company. -------------
        if financials is None or balance_sheet is None:
            raise DataError(
                f"No usable fundamentals for {symbol!r} from SEC EDGAR or yfinance."
            )

        # --- 5) Resolve the display name. Prefer EDGAR's registered name, then
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
