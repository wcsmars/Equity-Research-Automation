"""Offline tests for the yfinance market-data client and its hybrid hand-off.

yfinance ``Ticker`` objects are replaced with ``unittest.mock`` stand-ins that
carry hand-built ``.info`` dicts, ``fast_info`` fields and statement DataFrames
shaped like yfinance 1.x output (period-end Timestamp columns, newest first;
pretty row labels). Covered: minor-unit quote currencies (GBp), reporting vs
quote currency conversion for foreign issuers, the ``.info`` outage path,
multi-class share counts, the statement fallback's column and label handling,
and HybridProvider's note hand-off and market-data backfills.
No network access is needed. Run with:  python -m unittest tests.test_market_data
"""

from __future__ import annotations

import unittest
from unittest import mock

import numpy as np
import pandas as pd

from equity_valuation.data.base import DataError
from equity_valuation.data.market import YFinanceClient, major_currency
from equity_valuation.data.provider import HybridProvider
from equity_valuation.schemas import AnnualFinancials, BalanceSheetSnapshot

NaN = np.nan
COLS = pd.to_datetime(["2024-12-31", "2023-12-31", "2022-12-31", "2021-12-31", "2020-12-31"])


def _frame(rows: dict, scale: float = 1.0, keep_unscaled: tuple = ()) -> pd.DataFrame:
    df = pd.DataFrame(rows, index=COLS).T.astype(float)
    scaled = [r for r in df.index if r not in keep_unscaled]
    df.loc[scaled] *= scale
    return df


def income(scale: float = 1.0, **over) -> pd.DataFrame:
    """Four full years plus a sparse oldest (2020) column, as yfinance 1.x returns."""
    rows = {
        "Total Revenue": [1000, 900, 800, 700, 600],
        "EBIT": [230, 205, 180, 160, NaN],
        "Operating Income": [200, 180, 160, 140, NaN],
        "Pretax Income": [210, 190, 170, 150, NaN],
        "Tax Provision": [42, 38, -34, 30, NaN],
        "Interest Expense": [20, 15, 10, 10, NaN],
        "Net Income": [168, 152, 136, 120, NaN],
        "Reconciled Depreciation": [50, 45, 40, 35, NaN],
        "Diluted Average Shares": [10, 10, 10, 10, NaN],
        "Basic Average Shares": [10, 10, 10, 10, NaN],
    }
    rows.update(over)
    return _frame(rows, scale, keep_unscaled=("Diluted Average Shares", "Basic Average Shares"))


def cashflow(scale: float = 1.0) -> pd.DataFrame:
    return _frame({
        "Capital Expenditure": [-60, -55, -50, -45, NaN],
        "Cash Dividends Paid": [-40, -38, -36, -34, NaN],
        "Change In Working Capital": [-10, -8, -6, -5, NaN],
    }, scale)


def balance(scale: float = 1.0, drop: tuple = (), **over) -> pd.DataFrame:
    rows = {
        "Total Debt": [300, 280, 260, 240, NaN],
        "Cash And Cash Equivalents": [100, 90, 80, 70, NaN],
        "Other Short Term Investments": [50, 40, 30, 20, NaN],
        "Cash Cash Equivalents And Short Term Investments": [150, 130, 110, 90, NaN],
        "Stockholders Equity": [800, 700, 600, 500, NaN],
        "Common Stock Equity": [800, 700, 600, 500, NaN],
        "Total Equity Gross Minority Interest": [850, 750, 650, 550, NaN],
        "Minority Interest": [50, 50, 50, 50, NaN],
    }
    rows.update(over)
    return _frame(rows, scale).drop(index=list(drop))


def fast(last_price=None, currency=None, shares=None):
    """yfinance FastInfo stand-in (attribute access; missing fields -> None)."""
    return mock.NonCallableMock(spec=["last_price", "currency", "shares"],
                                last_price=last_price, currency=currency, shares=shares)


def ticker(info=None, fast_info=None, fin=None, cf=None, bs=None, info_error=None):
    """A mocked yfinance Ticker."""
    tk = mock.NonCallableMock(spec=["info", "fast_info", "financials", "cashflow", "balance_sheet"])
    if info_error is not None:
        type(tk).info = mock.PropertyMock(side_effect=info_error)
    else:
        tk.info = info if info is not None else {}
    tk.fast_info = fast_info if fast_info is not None else fast()
    tk.financials = fin if fin is not None else pd.DataFrame()
    tk.cashflow = cf if cf is not None else pd.DataFrame()
    tk.balance_sheet = bs if bs is not None else pd.DataFrame()
    return tk


def client(tickers: dict) -> YFinanceClient:
    """YFinanceClient whose yf.Ticker lookups hit `tickers` (unknown -> KeyError)."""
    c = YFinanceClient()
    c._ticker = mock.Mock(side_effect=lambda sym: tickers[sym])
    return c


def fx_ticker(rate: float):
    return ticker({}, fast(last_price=rate))


class MinorUnitCurrencyTests(unittest.TestCase):
    def test_minor_unit_codes_map_to_major_currency(self) -> None:
        self.assertEqual(major_currency("GBp"), ("GBP", 0.01))
        self.assertEqual(major_currency("GBX"), ("GBP", 0.01))
        self.assertEqual(major_currency("ZAc"), ("ZAR", 0.01))
        self.assertEqual(major_currency("ILA"), ("ILS", 0.01))

    def test_major_codes_are_unchanged(self) -> None:
        self.assertEqual(major_currency("GBP"), ("GBP", 1.0))
        self.assertEqual(major_currency("usd"), ("USD", 1.0))
        self.assertEqual(major_currency(None), (None, 1.0))


class MarketDataTests(unittest.TestCase):
    def test_pence_quote_is_converted_to_pounds(self) -> None:
        # marketCap arrives in pounds while the price is in pence.
        info = {"currency": "GBp", "currentPrice": 2500.0, "sharesOutstanding": 10.0,
                "marketCap": 250.0, "fiftyTwoWeekLow": 2000.0, "fiftyTwoWeekHigh": 3000.0,
                "dividendRate": 100.0, "trailingAnnualDividendYield": 0.04}
        md = client({"SHEL.L": ticker(info)}).get_market_data("SHEL.L")
        self.assertEqual(md.currency, "GBP")
        self.assertAlmostEqual(md.price, 25.0)
        self.assertAlmostEqual(md.market_cap, 250.0)
        self.assertAlmostEqual(md.shares_outstanding, 10.0)
        self.assertAlmostEqual(md.fifty_two_week_low, 20.0)
        self.assertAlmostEqual(md.fifty_two_week_high, 30.0)
        self.assertAlmostEqual(md.dividend_per_share, 1.0)
        self.assertTrue(any("GBp" in n for n in md._source_notes))

    def test_pence_market_cap_and_pound_dividend_are_detected(self) -> None:
        # The other reading of each field: marketCap in pence, dividend in pounds.
        info = {"currency": "GBp", "currentPrice": 2500.0, "sharesOutstanding": 10.0,
                "marketCap": 25000.0, "dividendRate": 1.0, "trailingAnnualDividendYield": 0.04}
        md = client({"X.L": ticker(info)}).get_market_data("X.L")
        self.assertAlmostEqual(md.market_cap, 250.0)
        self.assertAlmostEqual(md.dividend_per_share, 1.0)

    def test_info_outage_uses_fast_info_and_never_fabricates_a_cap(self) -> None:
        tk = ticker(info_error=RuntimeError("HTTP 401"),
                    fast_info=fast(last_price=40.0, currency="USD", shares=5.0))
        md = client({"X": tk}).get_market_data("X")
        self.assertEqual((md.price, md.shares_outstanding, md.market_cap), (40.0, 5.0, 200.0))
        self.assertTrue(any(".info) unavailable" in n for n in md._source_notes))

    def test_no_share_count_anywhere_leaves_zero_for_the_provider_to_fill(self) -> None:
        tk = ticker(info_error=RuntimeError("HTTP 429"), fast_info=fast(last_price=40.0))
        md = client({"X": tk}).get_market_data("X")
        self.assertEqual((md.shares_outstanding, md.market_cap), (0.0, 0.0))
        self.assertIsNone(md.dividend_per_share)

    def test_no_price_still_raises(self) -> None:
        with self.assertRaises(DataError):
            client({"X": ticker({"currency": "USD"})}).get_market_data("X")

    def test_multi_class_share_count_is_replaced_by_market_cap_over_price(self) -> None:
        info = {"currency": "USD", "currentPrice": 100.0, "sharesOutstanding": 48.0,
                "marketCap": 10_000.0}
        md = client({"GOOGL": ticker(info)}).get_market_data("GOOGL")
        self.assertAlmostEqual(md.shares_outstanding, 100.0)
        self.assertAlmostEqual(md.market_cap, 10_000.0)
        self.assertTrue(any("marketCap/price" in n for n in md._source_notes))

    def test_consistent_share_count_is_kept(self) -> None:
        info = {"currency": "USD", "currentPrice": 100.0, "sharesOutstanding": 99.0,
                "marketCap": 10_000.0}
        md = client({"X": ticker(info)}).get_market_data("X")
        self.assertEqual(md.shares_outstanding, 99.0)
        self.assertEqual(md._source_notes, [])

    def test_indicated_dividend_rate_is_d0_with_trailing_as_fallback(self) -> None:
        # Deliberate: the indicated rate reflects cuts and excludes specials.
        info = {"currency": "USD", "currentPrice": 50.0, "sharesOutstanding": 1.0,
                "dividendRate": 1.0, "trailingAnnualDividendRate": 1.75}
        self.assertEqual(client({"X": ticker(info)}).get_market_data("X").dividend_per_share, 1.0)
        del info["dividendRate"]
        self.assertEqual(client({"X": ticker(info)}).get_market_data("X").dividend_per_share, 1.75)


class FxRateTests(unittest.TestCase):
    def test_direct_pair(self) -> None:
        c = client({"TWDUSD=X": fx_ticker(0.03125)})
        self.assertEqual(c.get_fx_rate("TWD", "USD"), (0.03125, "TWDUSD=X"))

    def test_inverse_pair(self) -> None:
        rate, how = client({"USDTWD=X": fx_ticker(32.0)}).get_fx_rate("TWD", "USD")
        self.assertAlmostEqual(rate, 1 / 32.0)
        self.assertEqual(how, "1/USDTWD=X")

    def test_cross_through_usd(self) -> None:
        c = client({"DKKUSD=X": fx_ticker(0.15), "USDEUR=X": fx_ticker(0.9)})
        rate, how = c.get_fx_rate("DKK", "EUR")
        self.assertAlmostEqual(rate, 0.135)
        self.assertEqual(how, "DKKUSD=X x USDEUR=X")

    def test_same_currency_and_unavailable_rate(self) -> None:
        c = client({})
        self.assertEqual(c.get_fx_rate("usd", "USD"), (1.0, "same currency"))
        self.assertIsNone(c.get_fx_rate("TWD", "USD"))


class StatementFallbackTests(unittest.TestCase):
    def _fallback(self, info=None, fin=None, cf=None, bs=None, extra=None):
        tickers = {"X": ticker(info if info is not None else {"currency": "USD",
                                                               "financialCurrency": "USD"},
                               fin=fin if fin is not None else income(),
                               cf=cf if cf is not None else cashflow(),
                               bs=bs if bs is not None else balance())}
        tickers.update(extra or {})
        return client(tickers).get_annual_financials_fallback("X")

    def test_sparse_oldest_column_is_dropped(self) -> None:
        fin, _bs = self._fallback()
        self.assertEqual(fin.fiscal_years, [2021, 2022, 2023, 2024])
        self.assertEqual(fin.capex, [45.0, 50.0, 55.0, 60.0])
        self.assertTrue(any("dropped 2020-12-31" in n for n in fin._source_notes))

    def test_operating_income_is_preferred_over_yahoo_ebit(self) -> None:
        fin, _bs = self._fallback()
        self.assertEqual(fin.ebit, [140.0, 160.0, 180.0, 200.0])

    def test_nan_in_preferred_row_falls_through_per_period(self) -> None:
        fin, _bs = self._fallback(fin=income(**{"Diluted Average Shares": [10, 10, 10, NaN, NaN],
                                                "Basic Average Shares": [10, 10, 10, 9, NaN]}))
        self.assertEqual(fin.diluted_shares, [9.0, 10.0, 10.0, 10.0])
        bs_frame = balance()
        bs_frame.loc["Stockholders Equity", COLS[0]] = NaN
        _fin, bs = self._fallback(bs=bs_frame)
        self.assertEqual(bs.total_equity, 800.0)  # from Common Stock Equity

    def test_missing_line_is_zero_filled_with_a_note(self) -> None:
        fin, _bs = self._fallback(fin=income(**{"Reconciled Depreciation": [NaN] * 5}))
        self.assertEqual(fin.dep_amort, [0.0] * 4)
        self.assertIn("yfinance fallback: D&A unavailable; filled with 0.0", fin._source_notes)

    def test_tax_benefit_keeps_its_sign(self) -> None:
        fin, _bs = self._fallback()
        self.assertEqual(fin.tax_expense, [30.0, -34.0, 38.0, 42.0])

    def test_combined_cash_line_is_not_double_counted(self) -> None:
        _fin, bs = self._fallback(bs=balance(drop=("Cash And Cash Equivalents",)))
        self.assertEqual(bs.cash_and_investments, 150.0)
        _fin, bs = self._fallback(bs=balance(drop=("Cash Cash Equivalents And Short Term Investments",)))
        self.assertEqual(bs.cash_and_investments, 150.0)

    def test_gross_equity_line_excludes_minority_interest(self) -> None:
        _fin, bs = self._fallback(bs=balance(drop=("Stockholders Equity", "Common Stock Equity")))
        self.assertEqual(bs.total_equity, 800.0)

    def test_foreign_statements_are_converted_to_the_quote_currency(self) -> None:
        info = {"currency": "USD", "financialCurrency": "TWD"}
        fin, bs = self._fallback(info=info, fin=income(32), cf=cashflow(32), bs=balance(32),
                                 extra={"TWDUSD=X": fx_ticker(1 / 32)})
        self.assertEqual(fin.revenue, [700.0, 800.0, 900.0, 1000.0])
        self.assertEqual(fin.dividends_paid, [34.0, 36.0, 38.0, 40.0])
        self.assertEqual(fin.diluted_shares, [10.0] * 4)  # share counts untouched
        self.assertEqual((bs.total_debt, bs.cash_and_investments, bs.total_equity,
                          bs.minority_interest), (300.0, 150.0, 800.0, 50.0))
        self.assertTrue(fin._source_notes[0].startswith(
            "Fundamentals converted from TWD to USD at spot 0.03125"))

    def test_missing_fx_rate_leaves_statements_unconverted_with_warning(self) -> None:
        info = {"currency": "USD", "financialCurrency": "TWD"}
        fin, _bs = self._fallback(info=info, fin=income(32), cf=cashflow(32), bs=balance(32))
        self.assertEqual(fin.revenue[-1], 32000.0)
        self.assertTrue(fin._source_notes[0].startswith("WARNING: financial statements are in TWD"))
        self.assertIn("not comparable", fin._source_notes[0])

    def test_pence_quote_with_pound_statements_needs_no_fx(self) -> None:
        fin, _bs = self._fallback(info={"currency": "GBp", "financialCurrency": "GBP"})
        self.assertEqual(fin.revenue[-1], 1000.0)
        self.assertFalse(any("convert" in n or "WARNING" in n for n in fin._source_notes))

    def test_pence_quote_with_dollar_statements_converts_to_pounds(self) -> None:
        fin, _bs = self._fallback(info={"currency": "GBp", "financialCurrency": "USD"},
                                  extra={"USDGBP=X": fx_ticker(0.8)})
        self.assertAlmostEqual(fin.revenue[-1], 800.0)

    def test_january_period_end_takes_the_prior_fiscal_year(self) -> None:
        cols = pd.to_datetime(["2023-12-31", "2023-01-01", "2022-01-02"])
        fin_frame = pd.DataFrame({"Total Revenue": [3.0, 2.0, 1.0], "Net Income": [1.0] * 3},
                                 index=cols).T
        fin, _bs = self._fallback(fin=fin_frame, cf=pd.DataFrame(), bs=pd.DataFrame())
        self.assertEqual(fin.fiscal_years, [2021, 2022, 2023])


class HybridProviderMarketTests(unittest.TestCase):
    def _edgar_fails(self):
        edgar = mock.Mock()
        edgar.get_annual_financials.side_effect = DataError("not in SEC map")
        return edgar

    def test_foreign_adr_is_valued_in_one_currency(self) -> None:
        info = {"currency": "USD", "financialCurrency": "TWD", "currentPrice": 200.0,
                "sharesOutstanding": 10.0, "marketCap": 2000.0, "dividendRate": 4.0}
        c = client({"TSM": ticker(info, fin=income(32), cf=cashflow(32), bs=balance(32)),
                    "TWDUSD=X": fx_ticker(1 / 32)})
        cd = HybridProvider(edgar=self._edgar_fails(), market=c).get_company_data("TSM")
        self.assertEqual(cd.market.currency, "USD")
        self.assertEqual(cd.financials.revenue[-1], 1000.0)
        self.assertEqual(cd.balance_sheet.total_debt, 300.0)
        self.assertEqual(cd.source_notes[:2], ["EDGAR unavailable: not in SEC map",
                                               "Fundamentals: yfinance fallback"])
        self.assertTrue(any("converted from TWD to USD" in n for n in cd.source_notes))

    def test_unconverted_currencies_warning_leads_the_notes(self) -> None:
        info = {"currency": "USD", "financialCurrency": "TWD", "currentPrice": 200.0,
                "sharesOutstanding": 10.0, "marketCap": 2000.0}
        c = client({"TSM": ticker(info, fin=income(32), cf=cashflow(32), bs=balance(32))})
        cd = HybridProvider(edgar=self._edgar_fails(), market=c).get_company_data("TSM")
        self.assertTrue(cd.source_notes[2].startswith("WARNING: financial statements are in TWD"))

    def test_info_outage_backfills_shares_cap_and_dps_from_statements(self) -> None:
        fin = AnnualFinancials(
            fiscal_years=[2023, 2024], revenue=[90.0, 100.0], ebit=[9.0, 10.0],
            ebitda=[12.0, 13.0], net_income=[7.0, 8.0], dep_amort=[3.0, 3.0],
            capex=[4.0, 4.0], change_in_nwc=[1.0, 1.0], interest_expense=[1.0, 1.0],
            tax_expense=[2.0, 2.0], pretax_income=[9.0, 10.0], dividends_paid=[2.0, 3.0],
            diluted_shares=[20.0, 25.0])
        bs = BalanceSheetSnapshot(as_of="2024-12-31", total_debt=50.0,
                                  cash_and_investments=10.0, total_equity=80.0)
        edgar = mock.Mock()
        edgar.get_annual_financials.return_value = (fin, bs, "0000000001", "Fixture Co")
        tk = ticker(info_error=RuntimeError("HTTP 401"), fast_info=fast(last_price=40.0))
        cd = HybridProvider(edgar=edgar, market=client({"FIX": tk})).get_company_data("FIX")
        self.assertEqual(cd.market.shares_outstanding, 25.0)
        self.assertEqual(cd.market.market_cap, 1000.0)
        self.assertAlmostEqual(cd.market.dividend_per_share, 3.0 / 25.0)
        notes = " | ".join(cd.source_notes)
        self.assertIn(".info) unavailable", notes)
        self.assertIn("market cap unavailable from Yahoo; set to price x shares", notes)

    def test_no_share_count_anywhere_warns(self) -> None:
        fin = AnnualFinancials(
            fiscal_years=[2024], revenue=[100.0], ebit=[10.0], ebitda=[13.0],
            net_income=[8.0], dep_amort=[3.0], capex=[4.0], change_in_nwc=[1.0],
            interest_expense=[1.0], tax_expense=[2.0], pretax_income=[10.0],
            dividends_paid=[0.0], diluted_shares=[0.0])
        bs = BalanceSheetSnapshot(as_of="2024-12-31", total_debt=50.0,
                                  cash_and_investments=10.0, total_equity=80.0)
        edgar = mock.Mock()
        edgar.get_annual_financials.return_value = (fin, bs, "0000000001", "Fixture Co")
        tk = ticker(info_error=RuntimeError("HTTP 401"), fast_info=fast(last_price=40.0))
        cd = HybridProvider(edgar=edgar, market=client({"FIX": tk})).get_company_data("FIX")
        self.assertEqual(cd.market.market_cap, 0.0)
        self.assertTrue(cd.source_notes[1].startswith("WARNING: market cap unavailable"),
                        cd.source_notes)


class SuggestPeersTests(unittest.TestCase):
    def test_no_network_call_and_no_peers(self) -> None:
        c = client({})
        self.assertEqual(c.suggest_peers("AAPL"), [])
        c._ticker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
