"""Offline tests for the EDGAR companyfacts normalizer.

A hand-built ``companyfacts`` payload exercises the rules that matter with
real filings: restated comparatives win, quarterly and non-annual forms are
ignored, 52/53-week years are accepted, and a company that switched revenue
tags still gets one complete series with the preferred tag winning on overlap.
Further fixtures cover the full `get_annual_financials` path: EBIT fallbacks,
signed tax, stale balance-sheet facts, debt assembly, preferred stock, stale
revenue axes, amendments, and the hand-off of parsing notes to HybridProvider.
No network access is needed. Run with:  python -m unittest tests.test_edgar_parsing
"""

from __future__ import annotations

import unittest
from unittest import mock

from equity_valuation.data.base import DataError
from equity_valuation.data.edgar import _TAGS_REVENUE, EdgarClient
from equity_valuation.data.provider import HybridProvider
from equity_valuation.schemas import MarketData

PREFERRED = "RevenueFromContractWithCustomerExcludingAssessedTax"
FALLBACK = "Revenues"


def _entry(start: str, end: str, val: float, filed: str,
           fp: str = "FY", form: str = "10-K") -> dict:
    return {"start": start, "end": end, "val": val, "filed": filed,
            "fp": fp, "form": form, "fy": int(end[:4])}


def _facts(**tags: list[dict]) -> dict:
    return {"facts": {"us-gaap": {
        tag: {"units": {"USD": entries}} for tag, entries in tags.items()
    }}}


class AnnualFlowNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = EdgarClient(user_agent="equity-research-tests test@example.com")
        self.facts = _facts(**{
            PREFERRED: [
                _entry("2023-01-01", "2023-12-31", 300.0, "2024-02-01"),
                _entry("2022-01-01", "2022-12-31", 200.0, "2023-02-01"),
            ],
            FALLBACK: [
                # Original FY2021 report, then the restated comparative filed a
                # year later inside the FY2022 10-K.
                _entry("2021-01-01", "2021-12-31", 100.0, "2022-02-01"),
                _entry("2021-01-01", "2021-12-31", 111.0, "2023-02-01"),
                # Same period as the preferred tag; must lose to it.
                _entry("2023-01-01", "2023-12-31", 999.0, "2024-02-01"),
                # A 53-week fiscal year (371 days) is still an annual period.
                _entry("2019-12-26", "2020-12-31", 90.0, "2021-02-01"),
                # A one-quarter span labelled FY is not an annual flow.
                _entry("2019-10-01", "2019-12-31", 12.0, "2020-02-01"),
                # Quarterly and interim forms never feed the annual series.
                _entry("2018-01-01", "2018-12-31", 80.0, "2019-02-01", fp="Q4"),
                _entry("2017-01-01", "2017-12-31", 70.0, "2018-02-01", form="10-Q"),
            ],
        })
        self.series = self.client._annual_flow_by_fy(self.facts, (PREFERRED, FALLBACK))

    def test_latest_filing_wins_for_a_restated_period(self) -> None:
        self.assertEqual(self.series[2021], 111.0)

    def test_preferred_tag_wins_on_overlapping_periods(self) -> None:
        self.assertEqual(self.series[2023], 300.0)
        self.assertEqual(self.series[2022], 200.0)

    def test_fallback_tag_backfills_years_the_preferred_tag_lacks(self) -> None:
        self.assertEqual(self.series[2020], 90.0)

    def test_short_periods_and_non_annual_forms_are_dropped(self) -> None:
        self.assertNotIn(2019, self.series)
        self.assertNotIn(2018, self.series)
        self.assertNotIn(2017, self.series)

    def test_series_is_exactly_the_expected_years(self) -> None:
        self.assertEqual(self.series, {2020: 90.0, 2021: 111.0, 2022: 200.0, 2023: 300.0})

    def test_missing_tags_yield_an_empty_series(self) -> None:
        self.assertEqual(self.client._annual_flow_by_fy(self.facts, ("NoSuchTag",)), {})


# --------------------------------------------------------------------------- #
#  Full get_annual_financials fixtures
# --------------------------------------------------------------------------- #
_PRETAX = "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"
_YEARS = list(range(2019, 2025))
_SHARES = {"WeightedAverageNumberOfDilutedSharesOutstanding": [
    _entry(f"{y}-01-01", f"{y}-12-31", 400.0, f"{y + 1}-02-15") for y in _YEARS
]}


def _fy(year: int, val: float, filed: str = "", form: str = "10-K") -> dict:
    return _entry(f"{year}-01-01", f"{year}-12-31", val, filed or f"{year + 1}-02-15", form=form)


def _inst(end: str, val: float, filed: str = "", form: str = "10-K") -> dict:
    return {"end": end, "val": val, "filed": filed or f"{end[:4]}-12-31",
            "form": form, "fp": "FY"}


def _q2(end: str, val: float) -> dict:
    """An instant fact from a later 10-Q."""
    return {"end": end, "val": val, "filed": "2025-08-01", "form": "10-Q", "fp": "Q2"}


def _base_usd() -> dict:
    """Six clean fiscal years (2019-2024) plus a FY2024 balance sheet."""
    return {
        "Revenues": [_fy(y, 1000.0 + 100 * i) for i, y in enumerate(_YEARS)],
        "NetIncomeLoss": [_fy(y, 100.0 + 10 * i) for i, y in enumerate(_YEARS)],
        "OperatingIncomeLoss": [_fy(y, 150.0 + 10 * i) for i, y in enumerate(_YEARS)],
        "DepreciationDepletionAndAmortization": [_fy(y, 50.0) for y in _YEARS],
        "PaymentsToAcquirePropertyPlantAndEquipment": [_fy(y, 60.0) for y in _YEARS],
        "InterestExpense": [_fy(y, 10.0) for y in _YEARS],
        # A tax benefit (valuation-allowance release) in 2020.
        "IncomeTaxExpenseBenefit": [_fy(y, -60.0 if y == 2020 else 30.0) for y in _YEARS],
        _PRETAX: [_fy(y, 130.0 + 10 * i) for i, y in enumerate(_YEARS)],
        "PaymentsOfDividendsCommonStock": [_fy(y, 20.0) for y in _YEARS],
        "StockholdersEquity": [_inst("2024-12-31", 1000.0)],
        "CashAndCashEquivalentsAtCarryingValue": [_inst("2024-12-31", 200.0)],
        "LongTermDebtNoncurrent": [_inst("2024-12-31", 480.0)],
    }


def _company(usd: dict, shares: dict | None = None) -> dict:
    facts = _facts(**usd)
    for tag, entries in (shares if shares is not None else _SHARES).items():
        facts["facts"]["us-gaap"][tag] = {"units": {"shares": entries}}
    return facts


def _client(facts: dict) -> EdgarClient:
    client = EdgarClient(user_agent="equity-research-tests test@example.com")
    client._ticker_map = {"FIX": ("0000000001", "Fixture Co")}  # no directory fetch
    client.company_facts = mock.Mock(return_value=facts)        # no network
    return client


def _parse(usd: dict, shares: dict | None = None):
    fin, bs, _cik, _name = _client(_company(usd, shares)).get_annual_financials("FIX")
    return fin, bs, fin._source_notes


def _without(usd: dict, *tags: str) -> dict:
    return {k: v for k, v in usd.items() if k not in tags}


class FiscalYearLabelTests(unittest.TestCase):
    def test_52_53_week_years_ending_in_early_january_keep_their_own_label(self) -> None:
        # Saturday-nearest-Dec-31 calendar: three years end on Jan 1-3.
        ends = [("2017-12-31", "2018-12-30", 100.0), ("2018-12-31", "2019-12-29", 110.0),
                ("2019-12-30", "2021-01-03", 121.0), ("2021-01-04", "2022-01-02", 133.1),
                ("2022-01-03", "2023-01-01", 146.4), ("2023-01-02", "2023-12-31", 161.1)]
        facts = _facts(Revenues=[_entry(s, e, v, "2024-02-15") for s, e, v in ends])
        series = _client(facts)._annual_flow_by_fy(facts, ("Revenues",))
        self.assertEqual(series, {2018: 100.0, 2019: 110.0, 2020: 121.0,
                                  2021: 133.1, 2022: 146.4, 2023: 161.1})

    def test_late_january_year_end_keeps_the_calendar_year_label(self) -> None:
        facts = _facts(Revenues=[_entry("2023-02-01", "2024-01-31", 5.0, "2024-03-20")])
        self.assertEqual(_client(facts)._annual_flow_by_fy(facts, ("Revenues",)), {2024: 5.0})


class AmendmentAndPrecedenceTests(unittest.TestCase):
    def test_10k_amendment_restates_the_latest_year(self) -> None:
        usd = _base_usd()
        usd["Revenues"] = usd["Revenues"] + [_fy(2024, 1450.0, "2025-06-01", form="10-K/A")]
        fin, _bs, _notes = _parse(usd)
        self.assertEqual(fin.revenue[-1], 1450.0)

    def test_20f_amendment_and_40f_are_annual_forms(self) -> None:
        facts = _facts(Revenues=[_fy(2022, 7.0, form="40-F"),
                                 _fy(2023, 8.0, form="20-F"),
                                 _fy(2023, 9.0, "2024-09-01", form="20-F/A")])
        self.assertEqual(_client(facts)._annual_flow_by_fy(facts, ("Revenues",)),
                         {2022: 7.0, 2023: 9.0})

    def test_total_revenues_beats_the_asc606_subset(self) -> None:
        # A lessor/finance-arm filer: total Revenues includes lease income, the
        # ASC 606 contract-revenue tag does not.
        facts = _facts(**{
            "Revenues": [_fy(2023, 1000.0), _fy(2024, 1100.0)],
            PREFERRED: [_fy(2022, 280.0), _fy(2023, 300.0), _fy(2024, 320.0)],
        })
        series = _client(facts)._annual_flow_by_fy(facts, _TAGS_REVENUE)
        self.assertEqual(series, {2022: 280.0, 2023: 1000.0, 2024: 1100.0})


class AnnualFinancialsTests(unittest.TestCase):
    def test_clean_filer_parses_without_data_gap_notes(self) -> None:
        fin, bs, notes = _parse(_base_usd())
        self.assertEqual(fin.fiscal_years, _YEARS)
        self.assertEqual(fin.ebit, [150.0, 160.0, 170.0, 180.0, 190.0, 200.0])
        self.assertEqual(bs.total_debt, 480.0)
        self.assertFalse([n for n in notes if "EBIT" in n or "D&A" in n])

    def test_ebit_derived_from_pretax_plus_interest_when_operating_income_missing(self) -> None:
        fin, _bs, notes = _parse(_without(_base_usd(), "OperatingIncomeLoss"))
        self.assertEqual(fin.ebit, [140.0, 150.0, 160.0, 170.0, 180.0, 190.0])
        self.assertEqual(fin.ebitda, [e + 50.0 for e in fin.ebit])
        self.assertTrue(any("FY2019-2024; derived as pretax income + interest expense" in n
                            for n in notes), notes)

    def test_missing_latest_operating_income_is_derived_not_zero(self) -> None:
        usd = _base_usd()
        usd["OperatingIncomeLoss"] = [e for e in usd["OperatingIncomeLoss"]
                                      if not e["end"].startswith("2024")]
        fin, _bs, notes = _parse(usd)
        self.assertEqual(fin.ebit[-1], 190.0)  # pretax 180 + interest 10
        self.assertEqual(fin.ebit[:-1], [150.0, 160.0, 170.0, 180.0, 190.0])
        self.assertTrue(any("for FY2024; derived" in n for n in notes), notes)

    def test_ebit_from_revenue_minus_costs_when_no_pretax_income(self) -> None:
        usd = _without(_base_usd(), "OperatingIncomeLoss", _PRETAX)
        usd["CostsAndExpenses"] = [_fy(y, 900.0 + 100 * i) for i, y in enumerate(_YEARS)]
        fin, _bs, notes = _parse(usd)
        self.assertEqual(fin.ebit, [100.0] * 6)
        self.assertTrue(any("derived as revenue - CostsAndExpenses" in n for n in notes), notes)

    def test_ebit_without_any_source_is_zero_and_noted(self) -> None:
        fin, _bs, notes = _parse(_without(_base_usd(), "OperatingIncomeLoss", _PRETAX))
        self.assertEqual(fin.ebit, [0.0] * 6)
        self.assertIn("EBIT (operating income) unavailable on EDGAR; filled with 0.0", notes)

    def test_tax_benefit_keeps_its_sign(self) -> None:
        fin, _bs, _notes = _parse(_base_usd())
        self.assertEqual(fin.tax_expense, [30.0, -60.0, 30.0, 30.0, 30.0, 30.0])

    def test_partial_gap_in_a_series_is_noted(self) -> None:
        usd = _base_usd()
        usd["DepreciationDepletionAndAmortization"] = [_fy(y, 50.0) for y in (2019, 2020, 2021)]
        fin, _bs, notes = _parse(usd)
        self.assertEqual(fin.dep_amort, [50.0, 50.0, 50.0, 0.0, 0.0, 0.0])
        self.assertIn("D&A not reported on EDGAR for FY2022-2024; filled with 0.0", notes)

    def test_depreciation_backfills_years_without_a_d_and_a_tag(self) -> None:
        usd = _base_usd()
        usd["DepreciationDepletionAndAmortization"] = [_fy(y, 50.0) for y in (2019, 2020, 2021)]
        usd["Depreciation"] = [_fy(y, 45.0) for y in _YEARS]
        fin, _bs, notes = _parse(usd)
        self.assertEqual(fin.dep_amort, [50.0, 50.0, 50.0, 45.0, 45.0, 45.0])
        self.assertFalse([n for n in notes if n.startswith("D&A")], notes)

    def test_nwc_derived_from_working_capital_components(self) -> None:
        usd = _base_usd()
        usd["IncreaseDecreaseInAccountsReceivable"] = [_fy(y, 10.0) for y in _YEARS]
        usd["IncreaseDecreaseInInventories"] = [_fy(y, 5.0) for y in _YEARS]
        usd["IncreaseDecreaseInAccountsPayable"] = [_fy(y, 4.0) for y in _YEARS]
        fin, _bs, notes = _parse(usd)
        self.assertEqual(fin.change_in_nwc, [11.0] * 6)  # 10 + 5 - 4 (a cash use)
        self.assertTrue(any("derived from the receivables, inventories and payables" in n
                            for n in notes), notes)

    def test_aggregate_nwc_tag_wins_over_components(self) -> None:
        usd = _base_usd()
        usd["IncreaseDecreaseInOperatingCapital"] = [_fy(y, 7.0) for y in _YEARS]
        usd["IncreaseDecreaseInAccountsReceivable"] = [_fy(y, 10.0) for y in _YEARS]
        fin, _bs, _notes = _parse(usd)
        self.assertEqual(fin.change_in_nwc, [7.0] * 6)

    def test_revenue_moved_to_an_unread_tag_is_rejected_as_stale(self) -> None:
        usd = {
            "SalesRevenueNet": [_fy(y, 1000.0) for y in range(2010, 2018)],
            "RevenuesNetOfInterestExpense": [_fy(y, 2000.0) for y in range(2018, 2025)],
            "NetIncomeLoss": [_fy(y, 100.0) for y in range(2010, 2025)],
        }
        with self.assertRaisesRegex(DataError, "FY2017 but net income to FY2024"):
            _parse(usd)

    def test_one_year_revenue_lag_is_noted(self) -> None:
        usd = _base_usd()
        usd["Revenues"] = usd["Revenues"][:-1]  # FY2024 revenue under an unread tag
        fin, _bs, notes = _parse(usd)
        self.assertEqual(fin.fiscal_years[-1], 2023)
        self.assertTrue(any("revenue on EDGAR ends FY2023" in n for n in notes), notes)


class BalanceSheetTests(unittest.TestCase):
    def _bs(self, **instants: list[dict]):
        usd = _base_usd()
        for tag in ("StockholdersEquity", "CashAndCashEquivalentsAtCarryingValue",
                    "LongTermDebtNoncurrent"):
            usd.pop(tag)
        usd.update(instants)
        _fin, bs, notes = _parse(usd)
        return bs, notes

    def test_lines_no_longer_reported_are_not_summed_into_today(self) -> None:
        bs, notes = self._bs(
            StockholdersEquity=[_inst("2024-12-31", 1000.0), _q2("2025-06-30", 1050.0)],
            CashAndCashEquivalentsAtCarryingValue=[_inst("2024-12-31", 200.0),
                                                   _q2("2025-06-30", 210.0)],
            ShortTermInvestments=[_inst("2015-12-31", 900.0), _inst("2016-12-31", 800.0)],
            MinorityInterest=[_inst("2019-12-31", 70.0)],
        )
        self.assertEqual(bs.as_of, "2025-06-30")
        self.assertEqual(bs.cash_and_investments, 210.0)
        self.assertEqual(bs.total_equity, 1050.0)
        self.assertEqual(bs.minority_interest, 0.0)
        self.assertTrue(any(n.startswith("short-term investments last reported 2016-12-31")
                            for n in notes), notes)

    def test_item_only_in_the_last_10k_is_used_at_a_later_10q_date(self) -> None:
        bs, notes = self._bs(
            StockholdersEquity=[_q2("2025-06-30", 1050.0)],
            CashAndCashEquivalentsAtCarryingValue=[_q2("2025-06-30", 210.0)],
            MinorityInterest=[_inst("2024-12-31", 70.0)],
        )
        self.assertEqual(bs.minority_interest, 70.0)
        self.assertIn("minority interest taken from 2024-12-31; not reported at the "
                      "2025-06-30 balance sheet", notes)

    def test_debt_current_is_not_added_to_its_own_components(self) -> None:
        # DebtCurrent 70 = short-term borrowings 40 + current LTD 30.
        bs, _notes = self._bs(
            StockholdersEquity=[_inst("2024-12-31", 1000.0)],
            LongTermDebtNoncurrent=[_inst("2024-12-31", 470.0)],
            LongTermDebtCurrent=[_inst("2024-12-31", 30.0)],
            DebtCurrent=[_inst("2024-12-31", 70.0)],
        )
        self.assertEqual(bs.total_debt, 540.0)

    def test_lease_inclusive_noncurrent_tag_is_combined_with_current_portion(self) -> None:
        bs, _notes = self._bs(
            StockholdersEquity=[_inst("2024-12-31", 1000.0)],
            LongTermDebt=[_inst("2024-12-31", 1000.0)],
            LongTermDebtAndCapitalLeaseObligations=[_inst("2024-12-31", 950.0)],
            LongTermDebtCurrent=[_inst("2024-12-31", 50.0)],
        )
        self.assertEqual(bs.total_debt, 1000.0)

    def test_commercial_paper_counts_as_debt(self) -> None:
        bs, _notes = self._bs(
            StockholdersEquity=[_inst("2024-12-31", 1000.0)],
            CommercialPaper=[_inst("2024-12-31", 10.0)],
            LongTermDebtCurrent=[_inst("2024-12-31", 10.9)],
            LongTermDebtNoncurrent=[_inst("2024-12-31", 85.8)],
        )
        self.assertAlmostEqual(bs.total_debt, 106.7)

    def test_current_fallback_tag_beats_a_stale_preferred_tag(self) -> None:
        bs, _notes = self._bs(
            StockholdersEquity=[_inst("2024-12-31", 1000.0)],
            ShortTermBorrowings=[_inst("2016-12-31", 300.0)],
            DebtCurrent=[_inst("2024-12-31", 20.0)],
            LongTermDebtNoncurrent=[_inst("2024-12-31", 400.0)],
        )
        self.assertEqual(bs.total_debt, 420.0)

    def test_long_term_debt_total_plus_short_term_borrowings(self) -> None:
        # LongTermDebt already includes its current maturities (50).
        bs, _notes = self._bs(
            StockholdersEquity=[_inst("2024-12-31", 1000.0)],
            LongTermDebt=[_inst("2024-12-31", 500.0)],
            LongTermDebtCurrent=[_inst("2024-12-31", 50.0)],
            ShortTermBorrowings=[_inst("2024-12-31", 25.0)],
        )
        self.assertEqual(bs.total_debt, 525.0)

    def test_preferred_stock_is_read(self) -> None:
        bs, _notes = self._bs(
            StockholdersEquity=[_inst("2024-12-31", 900.0)],
            PreferredStockValue=[_inst("2024-12-31", 250.0)],
        )
        self.assertEqual(bs.preferred_equity, 250.0)


class HybridProviderEdgarTests(unittest.TestCase):
    def _provider(self, usd: dict, market: MarketData, fx=None) -> HybridProvider:
        client = mock.Mock()
        client.get_market_data.return_value = market
        client.get_fx_rate.return_value = fx
        return HybridProvider(edgar=_client(_company(usd)), market=client)

    @staticmethod
    def _market(**kw) -> MarketData:
        base = dict(ticker="FIX", name="Fixture", currency="USD", price=10.0,
                    shares_outstanding=400.0, market_cap=4000.0, dividend_per_share=0.05)
        base.update(kw)
        return MarketData(**base)

    def test_edgar_parsing_notes_reach_company_source_notes(self) -> None:
        usd = _without(_base_usd(), "OperatingIncomeLoss", "DepreciationDepletionAndAmortization")
        cd = self._provider(usd, self._market()).get_company_data("FIX")
        self.assertEqual(cd.source_notes[0], "Fundamentals: SEC EDGAR (CIK 0000000001)")
        self.assertIn("D&A unavailable on EDGAR; filled with 0.0", cd.source_notes)
        self.assertTrue(any("derived as pretax income + interest expense" in n
                            for n in cd.source_notes), cd.source_notes)

    def test_missing_market_shares_cap_and_dps_are_backfilled_from_edgar(self) -> None:
        market = self._market(shares_outstanding=0.0, market_cap=0.0, dividend_per_share=None)
        cd = self._provider(_base_usd(), market).get_company_data("FIX")
        self.assertEqual(cd.market.shares_outstanding, 400.0)
        self.assertEqual(cd.market.market_cap, 4000.0)
        self.assertAlmostEqual(cd.market.dividend_per_share, 20.0 / 400.0)
        self.assertEqual(market.market_cap, 0.0)  # caller's object untouched
        joined = " | ".join(cd.source_notes)
        self.assertIn("FY2024 diluted weighted-average shares from EDGAR", joined)
        self.assertIn("market cap unavailable from Yahoo; set to price x shares", joined)
        self.assertIn("dividend per share unavailable from Yahoo", joined)

    def test_reported_zero_dividend_is_kept(self) -> None:
        cd = self._provider(_base_usd(), self._market(dividend_per_share=0.0)).get_company_data("FIX")
        self.assertEqual(cd.market.dividend_per_share, 0.0)

    def test_usd_statements_are_converted_for_a_non_usd_quote(self) -> None:
        market = self._market(currency="CAD", price=13.0, market_cap=5200.0)
        cd = self._provider(_base_usd(), market, fx=(1.3, "USDCAD=X")).get_company_data("FIX")
        self.assertAlmostEqual(cd.financials.revenue[-1], 1500.0 * 1.3)
        self.assertAlmostEqual(cd.balance_sheet.total_debt, 480.0 * 1.3)
        self.assertEqual(cd.financials.diluted_shares[-1], 400.0)
        self.assertTrue(any("converted from USD to CAD at spot 1.3" in n
                            for n in cd.source_notes), cd.source_notes)

    def test_missing_fx_rate_gives_a_leading_warning(self) -> None:
        market = self._market(currency="CAD", price=13.0, market_cap=5200.0)
        cd = self._provider(_base_usd(), market, fx=None).get_company_data("FIX")
        self.assertEqual(cd.financials.revenue[-1], 1500.0)
        self.assertTrue(cd.source_notes[1].startswith("WARNING: EDGAR statements are in USD"),
                        cd.source_notes)


if __name__ == "__main__":
    unittest.main()
