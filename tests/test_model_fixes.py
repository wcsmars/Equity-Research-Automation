"""Regression tests for model-layer audit fixes (WACC, DCF, DDM/FCFE,
sensitivity, engine blend and warnings).

Each test perturbs the synthetic company (``equity_valuation.data.synthetic``)
in the way a provider gap or an edge-case input would, so nothing touches the
network. Run with:  python -m unittest tests.test_model_fixes
"""

from __future__ import annotations

from dataclasses import replace
import math
import unittest

from equity_valuation import value_company
from equity_valuation.data.synthetic import DEMO_PEERS, SyntheticProvider, make_company
from equity_valuation.engine import _fallback_football_field
from equity_valuation.models.dcf import run_dcf
from equity_valuation.models.ddm_fcfe import (
    _dividend_cagr,
    _sustainable_growth,
    run_ddm,
    run_fcfe,
)
from equity_valuation.models.sensitivity import dcf_sensitivity
from equity_valuation.models.wacc import compute_wacc
from equity_valuation.schemas import DCFAssumptions, DDMAssumptions, MacroAssumptions
from equity_valuation.utils import series_cagr, trim_outliers

from tests.test_synthetic import make_distressed_company


class _OneCompanyProvider(SyntheticProvider):
    """SyntheticProvider that serves a caller-supplied (perturbed) company."""

    def __init__(self, company):
        self._company = company

    def get_company_data(self, ticker):
        return self._company


def _with_fin(company, **changes):
    return replace(company, financials=replace(company.financials, **changes))


def _with_market(company, **changes):
    return replace(company, market=replace(company.market, **changes))


def _with_bs(company, **changes):
    return replace(company, balance_sheet=replace(company.balance_sheet, **changes))


class WACCMarketCapFallbackTests(unittest.TestCase):
    def setUp(self):
        self.company = make_company()
        self.macro = MacroAssumptions()
        self.base = compute_wacc(self.company, self.macro)

    def test_zero_market_cap_uses_price_times_shares(self):
        w = compute_wacc(_with_market(self.company, market_cap=0.0), self.macro)
        self.assertAlmostEqual(w.wacc, self.base.wacc, places=12)
        self.assertIn("market_cap unavailable; using price x shares_outstanding", w.detail["notes"])

    def test_zero_market_cap_and_shares_uses_diluted_shares(self):
        c = _with_market(self.company, market_cap=0.0, shares_outstanding=0.0)
        w = compute_wacc(c, self.macro)
        self.assertAlmostEqual(w.wacc, self.base.wacc, places=12)
        self.assertIn("market_cap unavailable; using price x latest diluted_shares", w.detail["notes"])
        # The DCF no longer inflates on the degraded market data.
        dcf = run_dcf(c, self.macro, DCFAssumptions(), c.market.price)
        base = run_dcf(self.company, self.macro, DCFAssumptions(), c.market.price)
        self.assertAlmostEqual(dcf.implied_price, base.implied_price, places=9)

    def test_no_equity_value_defaults_to_all_equity_not_all_debt(self):
        c = _with_market(self.company, market_cap=None, shares_outstanding=0.0)
        c = _with_fin(c, diluted_shares=[0.0] * 5)
        w = compute_wacc(c, self.macro)
        self.assertEqual((w.weight_equity, w.weight_debt), (1.0, 0.0))
        self.assertAlmostEqual(w.wacc, w.cost_of_equity, places=12)
        self.assertTrue(any("all-equity" in n for n in w.detail["notes"]))

    def test_debt_free_company_has_no_cost_of_debt_note(self):
        w = compute_wacc(_with_bs(self.company, total_debt=0.0), self.macro)
        self.assertEqual(w.weight_debt, 0.0)
        self.assertEqual(w.detail["notes"], [])


class DCFInputGapTests(unittest.TestCase):
    def setUp(self):
        self.company = make_company()
        self.macro = MacroAssumptions()
        self.price = self.company.market.price
        self.base = run_dcf(self.company, self.macro, DCFAssumptions(), self.price)

    def test_no_positive_revenue_fails_instead_of_negative_price(self):
        c = _with_fin(self.company, revenue=[0.0] * 5)
        with self.assertRaises(ValueError):
            run_dcf(c, self.macro, DCFAssumptions(), self.price)
        report = value_company("SYNT", provider=_OneCompanyProvider(c), peers=DEMO_PEERS)
        self.assertIsNone(report.dcf)
        self.assertNotIn("DCF", report.summary["methods"])
        self.assertTrue(any(w.startswith("DCF failed:") for w in report.warnings))

    def test_zero_filled_oldest_revenue_keeps_history_cagr(self):
        rev = list(self.company.financials.revenue)
        c = _with_fin(self.company, revenue=[0.0] + rev[1:])
        dcf = run_dcf(c, self.macro, DCFAssumptions(), self.price)
        fcfe = run_fcfe(c, self.macro, DDMAssumptions(), self.price)
        self.assertAlmostEqual(dcf.assumptions["revenue_growth_path"][0], 0.08, places=9)
        self.assertAlmostEqual(fcfe.detail["revenue_growth_path"][0], 0.08, places=9)

    def test_interior_gap_counts_the_full_period(self):
        rev = list(self.company.financials.revenue)
        c = _with_fin(self.company, revenue=[rev[0], None, rev[2], None, rev[4]])
        dcf = run_dcf(c, self.macro, DCFAssumptions(), self.price)
        self.assertAlmostEqual(dcf.assumptions["revenue_growth_path"][0], 0.08, places=9)
        # Fiscal years take precedence over positions when they are aligned.
        self.assertAlmostEqual(series_cagr([100.0, 0.0, 121.0], [2020, 2021, 2022]), 0.10)
        self.assertAlmostEqual(series_cagr([100.0, 121.0], [2019, 2021]), 0.10)

    def test_nwc_ratio_is_pooled_not_a_mean_of_ratios(self):
        rev = [100e9, 110e9, 110.5e9, 121e9, 133e9]
        dnwc = [0.0, 1.4e9, 0.9e9, 1.5e9, 1.7e9]
        c = _with_fin(self.company, revenue=rev, change_in_nwc=dnwc)
        dcf = run_dcf(c, self.macro, DCFAssumptions(), self.price)
        pooled = sum(dnwc[1:]) / (rev[-1] - rev[0])
        self.assertAlmostEqual(dcf.assumptions["nwc_pct_revenue"], pooled, places=12)

    def test_zero_filled_ebit_rebuilt_from_pretax_plus_interest(self):
        c = _with_fin(self.company, ebit=[0.0] * 5)
        dcf = run_dcf(c, self.macro, DCFAssumptions(), self.price)
        # Synthetic pretax + interest reproduces EBIT exactly.
        self.assertAlmostEqual(dcf.implied_price, self.base.implied_price, places=6)
        self.assertTrue(any("pretax income + interest" in n for n in dcf.assumptions["notes"]))

    def test_zero_effective_tax_rate_is_kept_but_flagged(self):
        c = _with_fin(self.company, tax_expense=[0.0] * 5)
        dcf = run_dcf(c, self.macro, DCFAssumptions(), self.price)
        self.assertEqual(dcf.assumptions["tax_rate"], 0.0)
        self.assertTrue(any("effective tax rate is 0%" in n for n in dcf.assumptions["notes"]))

    def test_zero_filled_capex_is_flagged(self):
        c = _with_fin(self.company, capex=[0.0] * 5)
        dcf = run_dcf(c, self.macro, DCFAssumptions(), self.price)
        self.assertIn("capex %revenue unavailable; defaulting to 0", dcf.assumptions["notes"])

    def test_terminal_method_is_normalised_or_falls_back_to_gordon(self):
        dcf = run_dcf(self.company, self.macro, DCFAssumptions(terminal_method=" Gordon "), self.price)
        self.assertEqual(dcf.implied_price, self.base.implied_price)
        self.assertEqual(dcf.assumptions["terminal_method"], "gordon")
        dcf = run_dcf(self.company, self.macro, DCFAssumptions(terminal_method="perpetuity"), self.price)
        self.assertEqual(dcf.implied_price, self.base.implied_price)
        self.assertTrue(any("unknown terminal_method" in n for n in dcf.assumptions["notes"]))
        exit_a = DCFAssumptions(terminal_method="Exit_Multiple", exit_ev_ebitda=12.0)
        dcf = run_dcf(self.company, self.macro, exit_a, self.price)
        self.assertEqual(dcf.assumptions["terminal_method"], "exit_multiple")

    def test_wacc_fallback_is_reported_on_the_result(self):
        c = _with_market(self.company, beta=-2.0)
        dcf = run_dcf(c, self.macro, DCFAssumptions(), self.price)
        self.assertGreater(dcf.wacc.wacc, 0)
        self.assertEqual(dcf.wacc.wacc, dcf.assumptions["wacc"])
        self.assertEqual(dcf.wacc.detail["wacc"], dcf.assumptions["wacc"])
        self.assertLess(dcf.wacc.detail["wacc_computed"], 0)

    def test_missing_cash_keeps_known_debt_and_none_debt_does_not_crash(self):
        c = _with_bs(self.company, cash_and_investments=float("nan"))
        dcf = run_dcf(c, self.macro, DCFAssumptions(), self.price)
        self.assertEqual(dcf.net_debt, self.company.balance_sheet.total_debt)
        c = _with_bs(self.company, total_debt=None)
        dcf = run_dcf(c, self.macro, DCFAssumptions(), self.price)
        self.assertEqual(dcf.net_debt, -self.company.balance_sheet.cash_and_investments)


class DDMFixTests(unittest.TestCase):
    def setUp(self):
        self.company = make_company()
        self.macro = MacroAssumptions()
        self.price = self.company.market.price
        self.base = run_ddm(self.company, self.macro, DDMAssumptions(), self.price)

    def test_negative_book_equity_skips_roe_growth(self):
        for equity in (-3.8e9, -20e9, 0.0):
            c = _with_bs(self.company, total_equity=equity)
            ddm = run_ddm(c, self.macro, DDMAssumptions(), self.price)
            # Falls back to the 8% dividend CAGR, same as with positive equity.
            self.assertAlmostEqual(ddm.detail["high_growth"], 0.08, places=9)
            self.assertAlmostEqual(ddm.implied_price, self.base.implied_price, places=9)
            self.assertTrue(any("Book equity" in n for n in ddm.detail["notes"]))

    def test_thin_book_equity_roe_is_not_used(self):
        c = _with_bs(self.company, total_equity=1e9)  # ROE ~2000%
        notes: list[str] = []
        self.assertIsNone(_sustainable_growth(c.financials, c, notes))
        self.assertTrue(any("not meaningful" in n for n in notes))

    def test_dividend_cagr_counts_gap_years(self):
        d = self.company.financials.dividends_paid[0]
        fin = replace(self.company.financials, dividends_paid=[d, 0.0, 0.0, 0.0, 1.2 * d])
        self.assertAlmostEqual(_dividend_cagr(fin), 1.2 ** 0.25 - 1.0, places=12)

    def test_zero_filled_dividends_paid_uses_dps_payout(self):
        c = _with_fin(self.company, dividends_paid=[0.0] * 5)
        notes: list[str] = []
        sg = _sustainable_growth(c.financials, c, notes)
        expected = _sustainable_growth(self.company.financials, self.company)  # 30% payout
        self.assertAlmostEqual(sg, expected, places=12)
        self.assertTrue(any("DPS x shares" in n for n in notes))

    def test_float_horizons_are_accepted(self):
        a = DDMAssumptions(high_growth_years=5.0, forecast_years=5.0)
        self.assertEqual(run_ddm(self.company, self.macro, a, self.price).implied_price,
                         self.base.implied_price)
        self.assertEqual(run_fcfe(self.company, self.macro, a, self.price).implied_price,
                         run_fcfe(self.company, self.macro, DDMAssumptions(), self.price).implied_price)


class FCFEFixTests(unittest.TestCase):
    def setUp(self):
        self.company = make_company()
        self.macro = MacroAssumptions()
        self.price = self.company.market.price

    def test_implausible_nwc_ratio_is_zeroed_like_the_dcf(self):
        rev = [100e9, 100.05e9, 100.1e9, 99.9e9, 100.2e9]
        c = _with_fin(self.company, revenue=rev, change_in_nwc=[1e9] * 5)
        fcfe = run_fcfe(c, self.macro, DDMAssumptions(), self.price)
        dcf = run_dcf(c, self.macro, DCFAssumptions(), self.price)
        self.assertEqual(fcfe.detail["nwc_pct_delta_revenue"], 0.0)
        self.assertEqual(dcf.assumptions["nwc_pct_revenue"], 0.0)
        self.assertTrue(any("implausible" in n for n in fcfe.detail["notes"]))

    def test_net_borrowing_holds_debt_to_revenue_constant(self):
        fcfe = run_fcfe(self.company, self.macro, DDMAssumptions(), self.price)
        d = fcfe.detail
        debt = self.company.balance_sheet.total_debt
        ratio = debt / self.company.financials.revenue[-1]
        prev_rev = self.company.financials.revenue[-1]
        for rev_t, fcfe_t in zip(d["revenue"], fcfe.fcfe):
            d_rev = rev_t - prev_rev
            operating = (d["net_margin"] + d["da_pct_revenue"] - d["capex_pct_revenue"]) * rev_t \
                - d["nwc_pct_delta_revenue"] * d_rev
            debt += fcfe_t - operating  # ΔDebt_t
            self.assertAlmostEqual(debt / rev_t, ratio, places=9)
            prev_rev = rev_t


class SensitivityFixTests(unittest.TestCase):
    def setUp(self):
        self.company = make_company()
        self.macro = MacroAssumptions()
        self.price = self.company.market.price

    def test_clamped_growth_cells_are_blank(self):
        wacc_grid, margin_grid = dcf_sensitivity(
            self.company, self.macro, DCFAssumptions(terminal_growth=0.075), self.price)
        base_wacc = wacc_grid.row_values[2]  # the zero-delta row
        for i, row in enumerate(wacc_grid.grid):
            for j, cell in enumerate(row):
                clamped = wacc_grid.row_values[i] - wacc_grid.col_values[j] < 0.01
                self.assertEqual(math.isnan(cell), clamped, (i, j))
        for row in margin_grid.grid:  # margin rows all discount at the base WACC
            for g, cell in zip(margin_grid.col_values, row):
                self.assertEqual(math.isnan(cell), base_wacc - g < 0.01, g)

    def test_wacc_fallback_rows_are_blank_and_labels_stay_ordered(self):
        c = _with_market(self.company, beta=-0.9)
        grid = dcf_sensitivity(c, self.macro, DCFAssumptions(), self.price)[0]
        self.assertEqual(grid.row_values, sorted(grid.row_values))
        for label, row in zip(grid.row_values, grid.grid):
            if label <= 0:
                self.assertTrue(all(math.isnan(p) for p in row))

    def test_exit_multiple_headline_sits_inside_the_dcf_bar(self):
        a = DCFAssumptions(terminal_method="exit_multiple", exit_ev_ebitda=30.0)
        report = value_company("SYNT", provider=SyntheticProvider(), peers=DEMO_PEERS,
                               dcf_assumptions=a)
        bar = next(r for r in report.football_field if r.method == "DCF")
        self.assertEqual(bar.base, report.dcf.implied_price)
        self.assertLessEqual(bar.low, bar.base)
        self.assertLessEqual(bar.base, bar.high)
        for s in report.sensitivities:
            self.assertIn("Gordon terminal", s.title)

    def test_gordon_headline_grids_are_unlabelled_and_centred(self):
        report = value_company("SYNT", provider=SyntheticProvider(), peers=DEMO_PEERS)
        grid = report.sensitivities[0]
        self.assertNotIn("Gordon terminal", grid.title)
        self.assertAlmostEqual(grid.grid[2][2], report.dcf.implied_price, places=9)


class EngineBlendAndWarningTests(unittest.TestCase):
    def test_demo_run_adds_no_model_warnings(self):
        report = value_company("SYNT", provider=SyntheticProvider(), peers=DEMO_PEERS)
        self.assertEqual(report.warnings, ["Synthetic demo data: no live market or filing data."])

    def test_placeholder_prices_are_excluded_from_the_blend(self):
        c = _with_fin(make_company(), revenue=[0.0] * 5)
        report = value_company("SYNT", provider=_OneCompanyProvider(c), peers=DEMO_PEERS)
        methods = report.summary["methods"]
        self.assertEqual(methods["FCFE"], 0.0)  # still shown per method
        positive = sorted(v for v in methods.values() if v > 0)
        self.assertAlmostEqual(report.summary["blended_target"],
                               (positive[0] + positive[1]) / 2.0, places=9)
        self.assertIn("FCFE excluded from blended target: no valuation (0.00).",
                      report.warnings)
        self.assertTrue(any(w.startswith("FCFE: Insufficient revenue") for w in report.warnings))

    def test_negative_dcf_counts_as_zero_with_a_warning(self):
        c = make_distressed_company()
        report = value_company("DSTR", provider=_OneCompanyProvider(c), run_comps=False)
        methods = report.summary["methods"]
        self.assertLess(methods["DCF"], 0)
        # Negative equity is floored at zero, not dropped: median of [0, FCFE].
        self.assertAlmostEqual(report.summary["blended_target"], methods["FCFE"] / 2.0, places=9)
        self.assertTrue(any(w.startswith("DCF implies negative equity") for w in report.warnings))

    def test_model_notes_reach_warnings(self):
        report = value_company("SYNT", provider=SyntheticProvider(), peers=DEMO_PEERS,
                               dcf_assumptions=DCFAssumptions(terminal_growth=0.09))
        self.assertTrue(any(w.startswith("DCF: terminal growth") and "clamped" in w
                            for w in report.warnings))
        c = _with_market(make_company(), beta=None)
        report = value_company("SYNT", provider=_OneCompanyProvider(c), peers=DEMO_PEERS)
        self.assertIn("WACC: beta unavailable; using DEFAULT_BETA=1.0", report.warnings)

    def test_fallback_football_field_keeps_bands_ordered(self):
        c = make_distressed_company()
        report = value_company("DSTR", provider=_OneCompanyProvider(c), run_comps=False)
        rows = _fallback_football_field(report)
        self.assertTrue(any(r.method == "DCF" and r.base < 0 for r in rows))
        for r in rows:
            self.assertLessEqual(r.low, r.base, r.method)
            self.assertLessEqual(r.base, r.high, r.method)


class TrimOutlierTests(unittest.TestCase):
    def test_two_values_are_not_trimmed(self):
        self.assertEqual(trim_outliers([5.0, 30.0], 3.0), [5.0, 30.0])
        self.assertEqual(trim_outliers([2.0, 100.0], 3.0), [2.0, 100.0])
        self.assertEqual(trim_outliers([1.0, 10.0, 12.0, 11.0], 3.0), [10.0, 12.0, 11.0])


if __name__ == "__main__":
    unittest.main()
