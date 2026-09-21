"""Offline tests for the EDGAR companyfacts normalizer.

A hand-built ``companyfacts`` payload exercises the rules that matter with
real filings: restated comparatives win, quarterly and non-annual forms are
ignored, 52/53-week years are accepted, and a company that switched revenue
tags still gets one complete series with the preferred tag winning on overlap.
No network access is needed. Run with:  python -m unittest tests.test_edgar_parsing
"""

from __future__ import annotations

import unittest

from equity_valuation.data.edgar import EdgarClient

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


if __name__ == "__main__":
    unittest.main()
