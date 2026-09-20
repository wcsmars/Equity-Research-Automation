# Equity Research Automation

An equity valuation engine with a local research dashboard. It combines SEC
EDGAR fundamentals and Yahoo Finance market data, runs several valuation
methods, and exports the results to Excel and HTML. The dashboard adds editable
assumptions, filing analysis, saved research, and Word/PowerPoint exports.

## Valuation workflow

| Component | Implementation |
| --- | --- |
| Data | EDGAR annual financials, yfinance prices and peer multiples; yfinance fundamentals as a fallback |
| DCF | Forecast FCFF, CAPM/WACC, Gordon-growth or exit-multiple terminal value, enterprise-to-equity bridge |
| Comparables | Peer multiples, outlier trimming, median-based implied prices |
| DDM / FCFE | Dividend and equity cash-flow valuations discounted at cost of equity |
| Sensitivity | WACC/growth and margin/growth grids, plus valuation-range comparisons |
| Research | Filing retrieval, notes, watchlist, optional source-linked AI summaries and assumption suggestions |
| Exports | Excel model, interactive HTML report, Word memo, PowerPoint briefing |

The data-provider interface lets the models run on supplied data independently
of the live APIs. The offline checks use a synthetic dividend-paying company
and a distressed company to exercise the valuation calculations.

## Run the valuation engine

Use Python 3.11 or newer. Run these commands from this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export SEC_USER_AGENT='equity-research your.email@example.com'
python -m equity_valuation AAPL
```

Replace the example contact with your own before requesting EDGAR data.
The CLI reads environment variables; it does not automatically load `.env`.
Without `SEC_USER_AGENT`, EDGAR is skipped and the valuation provider attempts
its yfinance fallback. No API key is needed for these two data sources.

```bash
python -m equity_valuation MSFT --peers AAPL,GOOGL,META,AMZN \
  --rf 0.043 --erp 0.05 --terminal-growth 0.025 --forecast-years 6
python -m equity_valuation NVDA --terminal-method exit_multiple \
  --exit-ev-ebitda 18 --html
python -m equity_valuation --help
```

Exports go to `output/` by default. Use `--out DIR` to choose another directory.
The Excel DCF sheet contains formulas for selected calculations; other model
outputs are snapshots. The HTML report embeds its chart library for offline use.

## Run the dashboard

The dashboard also needs Node.js 22.12 or newer and npm.

```bash
cp .env.example .env
# Edit .env: set your SEC contact and any optional API keys.
./run_dev.sh
```

Open `http://127.0.0.1:3000`. The script installs Python dependencies, installs
frontend packages from the lockfile on first run, and starts FastAPI and Next.js.
Press Ctrl-C to stop both servers.

| Setting | Purpose |
| --- | --- |
| `SEC_USER_AGENT` | Application name and contact email for EDGAR requests |
| `FMP_API_KEY` | Optional Financial Modeling Prep news, estimates, ratios, and transcripts; coverage depends on the account plan |
| `ANTHROPIC_API_KEY` | Optional research summaries, chat, and research notes |
| `ANTHROPIC_MODEL` | Optional override of the model used by the research service |

The research assistant sends supplied text, PDFs, and valuation context to
Anthropic when invoked. Its suggestions require user review before applying
them to the model. Saved watchlists and notes stay in `data/copilot_store.json`;
that file, API keys, and generated exports are ignored by Git.

For manual startup after installing both Python requirement files and running
`npm ci` in `frontend/`:

```bash
set -a; source .env; set +a
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
# In another terminal:
cd frontend
npm run dev -- --hostname 127.0.0.1
```

A macOS Electron wrapper is in [desktop/](desktop/README.md). It uses the local
project's Python and Node installations; it is not a standalone distribution.

## Offline checks

```bash
python -m pip install -r requirements.txt -r backend/requirements-backend.txt
python -m tests.test_synthetic
python -m unittest discover -s tests -p 'test_*.py'
cd frontend
npm ci
npm run build
```

The synthetic script checks WACC bounds, valuation identities, sensitivity
outputs, and ordered valuation ranges including negative-value cases. The
unittest suite checks settings persistence and local-origin restrictions. These
checks do not establish live data accuracy or investment performance.

## Project layout

```text
equity_valuation/   Data providers, valuation models, CLI, Excel/HTML exporters
backend/           FastAPI routes, research integration, persistence, exports
frontend/          Next.js / React dashboard
desktop/           Electron wrapper for local macOS use
tests/             Offline model and settings checks
```

This is a local, single-user research tool without authentication. Live data can
be missing, stale, or rate-limited, and foreign-company coverage is less complete.
Default macro inputs are illustrative assumptions rather than current market
estimates. Review source data, peer selection, and assumptions before relying on
any valuation; model outputs and generated research are not investment advice.
