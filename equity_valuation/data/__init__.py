"""Data providers: SEC EDGAR fundamentals + yfinance market data, combined."""

from __future__ import annotations

from .base import DataError, DataProvider  # noqa: F401

__all__ = ["DataProvider", "DataError"]
