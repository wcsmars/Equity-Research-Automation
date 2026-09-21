"""Command-line interface.

    python -m equity_valuation AAPL
    python -m equity_valuation AAPL --peers MSFT,GOOGL,META --out output
    python -m equity_valuation MSFT --rf 0.043 --erp 0.05 --terminal-growth 0.025 \
        --forecast-years 6 --no-ddm --excel --html
"""

from __future__ import annotations

import argparse
import os
import sys

from . import config
from .schemas import DCFAssumptions, DDMAssumptions, MacroAssumptions


def _fmt_money(x, sym="$"):
    if x is None:
        return "n/a"
    return f"{sym}{x:,.2f}"


def _fmt_pct(x):
    if x is None:
        return "n/a"
    return f"{x * 100:+.1f}%"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="equity_valuation",
        description="Automated equity valuation: DCF, comps, DDM/FCFE, sensitivity "
        "-> Excel + HTML.",
    )
    p.add_argument("ticker", help="Target ticker, e.g. AAPL")
    p.add_argument(
        "--peers",
        default=None,
        help="Comma-separated peer tickers for comps (comps are skipped if omitted).",
    )
    p.add_argument("--out", default=config.DEFAULT_OUTPUT_DIR, help="Output directory.")

    # Macro / CAPM
    p.add_argument("--rf", type=float, default=config.DEFAULT_RISK_FREE_RATE,
                   help="Risk-free rate (decimal).")
    p.add_argument("--erp", type=float, default=config.DEFAULT_EQUITY_RISK_PREMIUM,
                   help="Equity risk premium (decimal).")
    p.add_argument("--tax", type=float, default=None,
                   help="Marginal tax rate (decimal); default derives effective.")
    p.add_argument("--cost-of-debt", type=float, default=None,
                   help="Pre-tax cost of debt (decimal); default derived.")

    # DCF
    p.add_argument("--forecast-years", type=int, default=config.DEFAULT_FORECAST_YEARS)
    p.add_argument("--terminal-growth", type=float, default=config.DEFAULT_TERMINAL_GROWTH)
    p.add_argument("--terminal-method", choices=["gordon", "exit_multiple"], default="gordon")
    p.add_argument("--exit-ev-ebitda", type=float, default=None,
                   help="Exit EV/EBITDA multiple (required if terminal-method=exit_multiple).")
    p.add_argument("--target-ebit-margin", type=float, default=None,
                   help="Terminal EBIT margin (decimal) to fade toward.")

    # Toggles
    p.add_argument("--no-dcf", action="store_true")
    p.add_argument("--no-comps", action="store_true")
    p.add_argument("--no-ddm", action="store_true")
    p.add_argument("--no-fcfe", action="store_true")
    p.add_argument("--no-sensitivity", action="store_true")

    # Output formats
    p.add_argument("--excel", action="store_true", help="Write Excel (default: on if neither flag).")
    p.add_argument("--html", action="store_true", help="Write HTML (default: on if neither flag).")
    p.add_argument("--quiet", action="store_true", help="Suppress the console summary.")
    return p


def _print_summary(report) -> None:
    s = report.summary
    sym = config.CURRENCY_SYMBOLS.get(s.get("currency"), "")
    line = "=" * 64
    print(line)
    print(f"  {s['name']}  ({s['ticker']})")
    print(f"  Current price: {_fmt_money(s['current_price'], sym)}   "
          f"Recommendation: {s['recommendation']}")
    print(line)
    print("  Method                 Implied price      Upside")
    print("  " + "-" * 50)
    for name, price in s["methods"].items():
        up = (price / s["current_price"] - 1.0) if (price and s["current_price"]) else None
        print(f"  {name:<22} {_fmt_money(price, sym):>14}   {_fmt_pct(up):>8}")
    print("  " + "-" * 50)
    print(f"  {'Blended target':<22} {_fmt_money(s['blended_target'], sym):>14}   "
          f"{_fmt_pct(s['blended_upside']):>8}")
    print(line)
    if report.warnings:
        print("  Notes:")
        for w in report.warnings:
            print(f"    - {w}")
        print(line)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    macro = MacroAssumptions(
        risk_free_rate=args.rf,
        equity_risk_premium=args.erp,
        tax_rate=args.tax,
        pretax_cost_of_debt=args.cost_of_debt,
    )
    dcf_assumptions = DCFAssumptions(
        forecast_years=args.forecast_years,
        terminal_growth=args.terminal_growth,
        terminal_method=args.terminal_method,
        exit_ev_ebitda=args.exit_ev_ebitda,
        target_ebit_margin=args.target_ebit_margin,
        tax_rate=args.tax,
    )
    ddm_assumptions = DDMAssumptions(forecast_years=args.forecast_years)
    peers = [t.strip().upper() for t in args.peers.split(",")] if args.peers else None

    from .engine import value_company

    try:
        report = value_company(
            args.ticker,
            macro=macro,
            dcf_assumptions=dcf_assumptions,
            ddm_assumptions=ddm_assumptions,
            peers=peers,
            run_dcf=not args.no_dcf,
            run_comps=not args.no_comps,
            run_ddm=not args.no_ddm,
            run_fcfe=not args.no_fcfe,
            run_sensitivity=not args.no_sensitivity,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: could not value {args.ticker}: {exc}", file=sys.stderr)
        return 1

    if not args.quiet:
        _print_summary(report)

    # Default: write both if neither flag is given.
    want_excel = args.excel or not (args.excel or args.html)
    want_html = args.html or not (args.excel or args.html)
    os.makedirs(args.out, exist_ok=True)
    ticker = report.company.ticker

    if want_excel:
        try:
            from .report.excel import write_excel

            path = write_excel(report, os.path.join(args.out, f"{ticker}_valuation.xlsx"))
            print(f"  Excel : {path}")
        except Exception as exc:  # noqa: BLE001
            print(f"  Excel export failed: {exc}", file=sys.stderr)

    if want_html:
        try:
            from .report.html import write_html

            path = write_html(report, os.path.join(args.out, f"{ticker}_valuation.html"))
            print(f"  HTML  : {path}")
        except Exception as exc:  # noqa: BLE001
            print(f"  HTML export failed: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
