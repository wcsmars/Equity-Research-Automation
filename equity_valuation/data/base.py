"""Abstract data-provider interface.

Concrete providers (EDGAR, yfinance, the hybrid combiner) implement this so the
engine never depends on a specific data source.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..schemas import CompanyData, CompRow, MarketData


class DataProvider(ABC):
    """Pulls and normalizes everything the valuation engine needs."""

    @abstractmethod
    def get_company_data(self, ticker: str) -> CompanyData:
        """Return fully-populated CompanyData for the target ticker.

        Must raise DataError (see exceptions) if the company cannot be resolved
        or essential fundamentals are unavailable.
        """

    @abstractmethod
    def get_market_data(self, ticker: str) -> MarketData:
        """Return live MarketData for a single ticker."""

    @abstractmethod
    def get_peer_comp_rows(self, tickers: list[str]) -> list[CompRow]:
        """Return trading-comps rows (multiples) for the given peer tickers.

        Tickers that cannot be resolved should be skipped, not raised on.
        """

    def suggest_peers(self, ticker: str) -> list[str]:
        """Best-effort list of peer tickers (same sector/industry). May be empty."""
        return []


class DataError(RuntimeError):
    """Raised when a provider cannot supply essential data."""
