"""Offline smoke + sanity tests using hand-built synthetic CompanyData.

Runs every model without touching the network, so it isolates the financial math
from flaky data sources. Run with:  python -m tests.test_synthetic
(from the project root).

The synthetic firm: steady grower, modestly levered, dividend payer, so DCF /
comps / DDM / FCFE all have something to chew on and we can assert on signs and
rough magnitudes.
"""

from __future__ import annotations

import math
import sys

from equity_valuation.schemas import (
    AnnualFinancials,
    BalanceSheetSnapshot,
    CompanyData,
    CompRow,
    DCFAssumptions,
    DDMAssumptions,
    MacroAssumptions,
    MarketData,
)


def _ramp(start: float, growth: float, n: int) -> list[float]:
    return [start * (1 + growth) ** i for i in range(n)]


def make_company() -> CompanyData:
    years = [2020, 2021, 2022, 2023, 2024]
    rev = _ramp(80_000_000_000.0, 0.08, 5)          # $80B growing 8%/yr
    ebit = [r * 0.25 for r in rev]                  # 25% EBIT margin
    da = [r * 0.04 for r in rev]
    ebitda = [e + d for e, d in zip(ebit, da)]
    pretax = [e * 0.95 for e in ebit]               # small interest drag
    tax = [p * 0.21 for p in pretax]
    ni = [p - t for p, t in zip(pretax, tax)]
    capex = [r * 0.05 for r in rev]
    dnwc = [r * 0.01 for r in rev]
    interest = [e * 0.05 for e in ebit]
    div = [n * 0.30 for n in ni]                    # 30% payout
    shares = [10_000_000_000.0] * 5

    fin = AnnualFinancials(
        fiscal_years=years, revenue=rev, ebit=ebit, ebitda=ebitda, net_income=ni,
        dep_amort=da, capex=capex, change_in_nwc=dnwc, interest_expense=interest,
        tax_expense=tax, pretax_income=pretax, dividends_paid=div, diluted_shares=shares,
    )
    bs = BalanceSheetSnapshot(
        as_of="2024-12-31", total_debt=20_000_000_000.0,
        cash_and_investments=8_000_000_000.0, total_equity=60_000_000_000.0,
    )
    eps = ni[-1] / shares[-1]
    price = eps * 20.0                              # ~20x trailing P/E
    mkt = MarketData(
        ticker="SYNT", name="Synthetic Corp", currency="USD", price=price,
        shares_outstanding=shares[-1], market_cap=price * shares[-1], beta=1.1,
        dividend_per_share=div[-1] / shares[-1], fifty_two_week_low=price * 0.8,
        fifty_two_week_high=price * 1.25, sector="Technology", industry="Software",
    )
    return CompanyData(ticker="SYNT", name="Synthetic Corp", cik=None,
                       financials=fin, balance_sheet=bs, market=mkt)


class _FakeProvider:
    """Minimal DataProvider stand-in serving synthetic peer multiples."""

    def get_company_data(self, ticker):  # not used here
        return make_company()

    def get_market_data(self, ticker):
        return make_company().market

    def suggest_peers(self, ticker):
        return ["PEER1", "PEER2", "PEER3"]

    def get_peer_comp_rows(self, tickers):
        base = [
            (18.0, 4.0, 19.0, 5.0, 1.6),
            (22.0, 5.5, 24.0, 6.5, 2.1),
            (20.0, 4.8, 21.0, 5.8, 1.8),
        ]
        rows = []
        for tk, (eve, evs, pe, pb, peg) in zip(tickers, base):
            rows.append(CompRow(ticker=tk, name=tk, market_cap=5e10,
                                enterprise_value=5.2e10, ev_ebitda=eve, ev_sales=evs,
                                pe=pe, pb=pb, peg=peg))
        return rows


def _ok(cond, msg):
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    return cond


def main() -> int:
    company = make_company()
    macro = MacroAssumptions()
    passed = True

    print("DCF:")
    from equity_valuation.models.dcf import run_dcf
    from equity_valuation.models.wacc import compute_wacc

    wacc = compute_wacc(company, macro)
    passed &= _ok(0.04 < wacc.wacc < 0.15, f"WACC in (4%,15%): {wacc.wacc:.4f}")
    passed &= _ok(wacc.cost_of_equity > wacc.after_tax_cost_of_debt,
                  "ke > after-tax kd")
    dcf = run_dcf(company, macro, DCFAssumptions(), company.market.price)
    passed &= _ok(dcf.implied_price > 0, f"implied price > 0: {dcf.implied_price:,.2f}")
    passed &= _ok(dcf.enterprise_value > dcf.equity_value,
                  "EV > equity (net debt positive)")
    passed &= _ok(abs(sum(dcf.pv_fcff) + dcf.pv_terminal - dcf.enterprise_value) < 1.0,
                  "EV == sum(PV FCFF) + PV terminal")
    passed &= _ok(0.3 < dcf.pv_terminal / dcf.enterprise_value < 0.95,
                  f"terminal value share reasonable: "
                  f"{dcf.pv_terminal / dcf.enterprise_value:.2%}")

    print("Comps:")
    from equity_valuation.models.comps import run_comps

    comps = run_comps(company, _FakeProvider(), None, company.market.price)
    passed &= _ok(len(comps.peers) == 3, f"3 peers: {len(comps.peers)}")
    passed &= _ok(comps.implied.get("ev_ebitda") and comps.implied["ev_ebitda"] > 0,
                  "EV/EBITDA implied price > 0")
    passed &= _ok(comps.implied_price_summary.get("median") is not None,
                  "comps median summary present")

    print("DDM / FCFE:")
    from equity_valuation.models.ddm_fcfe import run_ddm, run_fcfe

    ddm = run_ddm(company, macro, DDMAssumptions(), company.market.price)
    passed &= _ok(ddm is not None and ddm.implied_price > 0,
                  f"DDM implied price > 0: {ddm.implied_price:,.2f}" if ddm else "DDM None")
    fcfe = run_fcfe(company, macro, DDMAssumptions(), company.market.price)
    passed &= _ok(fcfe.implied_price > 0, f"FCFE implied price > 0: {fcfe.implied_price:,.2f}")

    print("Sensitivity / football field:")
    from equity_valuation.models.sensitivity import build_football_field, dcf_sensitivity

    sens = dcf_sensitivity(company, macro, DCFAssumptions(), company.market.price)
    passed &= _ok(len(sens) >= 1, f"at least one sensitivity grid: {len(sens)}")
    if sens:
        g = sens[0]
        finite = [v for row in g.grid for v in row if isinstance(v, float) and math.isfinite(v)]
        passed &= _ok(len(finite) > 0 and all(v > 0 for v in finite),
                      "sensitivity grid has positive finite prices")
        # Rows ascend in WACC, so every column must be non-increasing top to bottom.
        rows = g.grid
        pairs = [
            (rows[i][j], rows[i + 1][j])
            for i in range(len(rows) - 1)
            for j in range(len(g.col_values))
        ]
        finite_pairs = [(a, b) for a, b in pairs
                        if isinstance(a, float) and isinstance(b, float)
                        and math.isfinite(a) and math.isfinite(b)]
        passed &= _ok(finite_pairs and all(a >= b for a, b in finite_pairs),
                      f"higher WACC -> lower implied price ({len(finite_pairs)} grid pairs)")
    from equity_valuation.schemas import ValuationReport

    report = ValuationReport(company=company, macro=macro, current_price=company.market.price,
                             dcf=dcf, comps=comps, ddm=ddm, fcfe=fcfe, sensitivities=sens)
    ff = build_football_field(report)
    passed &= _ok(len(ff) >= 2, f"football field has >=2 rows: {len(ff)}")
    passed &= _ok(all(r.low <= r.base <= r.high for r in ff),
                  "every football-field row satisfies low <= base <= high")

    print("Distressed / negative-price edge cases:")
    passed &= _distressed_invariants()

    print("\n" + ("ALL PASSED" if passed else "SOME FAILED"))
    return 0 if passed else 1


def make_distressed_company() -> CompanyData:
    """Over-levered, thin-margin firm: drives DCF/FCFE equity value negative so the
    football-field band paths must still honor low <= base <= high."""
    years = [2020, 2021, 2022, 2023, 2024]
    rev = _ramp(50_000_000_000.0, 0.0, 5)
    ebit = [r * 0.03 for r in rev]                  # 3% margin
    da = [r * 0.04 for r in rev]
    ebitda = [e + d for e, d in zip(ebit, da)]
    pretax = [e * 0.2 for e in ebit]                # heavy interest drag
    tax = [max(p, 0) * 0.21 for p in pretax]
    ni = [p - t for p, t in zip(pretax, tax)]
    capex = [r * 0.06 for r in rev]
    dnwc = [0.0 for _ in rev]
    interest = [e * 0.8 for e in ebit]
    div = [0.0] * 5
    shares = [1_000_000_000.0] * 5
    fin = AnnualFinancials(
        fiscal_years=years, revenue=rev, ebit=ebit, ebitda=ebitda, net_income=ni,
        dep_amort=da, capex=capex, change_in_nwc=dnwc, interest_expense=interest,
        tax_expense=tax, pretax_income=pretax, dividends_paid=div, diluted_shares=shares,
    )
    bs = BalanceSheetSnapshot(
        as_of="2024-12-31", total_debt=400_000_000_000.0,   # net debt >> any plausible EV
        cash_and_investments=2_000_000_000.0, total_equity=5_000_000_000.0,
    )
    mkt = MarketData(
        ticker="DSTR", name="Distressed Co", currency="USD", price=4.0,
        shares_outstanding=shares[-1], market_cap=4.0 * shares[-1], beta=1.8,
        dividend_per_share=None, fifty_two_week_low=2.0, fifty_two_week_high=12.0,
        sector="Industrials", industry="Heavy",
    )
    return CompanyData(ticker="DSTR", name="Distressed Co", cik=None,
                       financials=fin, balance_sheet=bs, market=mkt)


def _distressed_invariants() -> bool:
    """Run the pipeline on a distressed firm; assert football-field invariants
    survive negative implied prices (the band fix), and nothing crashes."""
    from equity_valuation.models.dcf import run_dcf
    from equity_valuation.models.ddm_fcfe import run_fcfe
    from equity_valuation.models.sensitivity import build_football_field, dcf_sensitivity
    from equity_valuation.schemas import ValuationReport

    c = make_distressed_company()
    macro = MacroAssumptions()
    ok = True
    dcf = run_dcf(c, macro, DCFAssumptions(), c.market.price)
    ok &= _ok(dcf.equity_value < dcf.enterprise_value, "distressed: net debt drags equity below EV")
    fcfe = run_fcfe(c, macro, DDMAssumptions(), c.market.price)
    sens = dcf_sensitivity(c, macro, DCFAssumptions(), c.market.price)
    report = ValuationReport(company=c, macro=macro, current_price=c.market.price,
                             dcf=dcf, comps=None, ddm=None, fcfe=fcfe, sensitivities=sens)
    ff = build_football_field(report)
    ok &= _ok(all(r.low <= r.base <= r.high for r in ff),
              f"football-field invariant holds with negative prices ({len(ff)} rows)")
    try:
        from equity_valuation.models import sensitivity as _s
        if hasattr(_s, "_band"):
            lo, base, hi = _s._band(-10.0, 0.15)
            ok &= _ok(lo <= base <= hi, f"_band(-10,0.15) ordered: ({lo},{base},{hi})")
    except Exception as exc:  # noqa: BLE001
        print(f"  [info] could not probe _band directly: {exc}")
    return ok


if __name__ == "__main__":
    sys.exit(main())
