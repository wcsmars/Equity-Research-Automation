"""SEC EDGAR fundamentals provider.

Pulls normalized annual financials from the public, key-less SEC `data.sec.gov`
XBRL JSON API and maps the raw us-gaap facts into the package's canonical
`AnnualFinancials` / `BalanceSheetSnapshot` dataclasses.

Two HTTP endpoints are used (both require a descriptive `User-Agent` header or
the SEC returns HTTP 403):
  * ticker -> CIK directory: https://www.sec.gov/files/company_tickers.json
  * company facts:           https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json

Design conventions honored from schemas.py:
  * All amounts are absolute reporting-currency units (the USD facts are already
    absolute, so no scaling is applied).
  * `capex`, `dep_amort`, `interest_expense`, `dividends_paid` are stored as
    POSITIVE magnitudes regardless of XBRL sign. `tax_expense` keeps the XBRL
    sign of IncomeTaxExpenseBenefit (expense positive, a tax BENEFIT negative).
  * Annual series are ordered OLDEST -> NEWEST; the last element is most recent
    and aligns with the `BalanceSheetSnapshot`.

Only `revenue` or `net_income` being entirely unavailable (or years out of date
relative to the other) is fatal (raises `DataError`); every other gap degrades
gracefully (zeros or a derived value + a source note). The notes are attached to
the returned `AnnualFinancials` as ``_source_notes`` and the hybrid provider
copies them into `CompanyData.source_notes`.
"""

from __future__ import annotations

import time
from typing import Optional

import requests

from .. import config
from ..schemas import AnnualFinancials, BalanceSheetSnapshot
from ..utils import is_num
from .base import DataError


# --------------------------------------------------------------------------- #
#  Endpoints & parsing constants
# --------------------------------------------------------------------------- #
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# Annual filing forms we accept for FLOW (income/cash-flow) frames. Amendments
# (/A) carry restated XBRL and win through the latest-`filed` rule; 40-F is the
# Canadian MJDS annual report.
_ANNUAL_FORMS = ("10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A")

# A "full-year" period: end - start measured in days. We allow a generous band
# to absorb 52/53-week fiscal calendars and minor reporting drift.
_MIN_PERIOD_DAYS = 330
_MAX_PERIOD_DAYS = 400

# 52/53-week years that end on the weekend nearest Dec 31 sometimes end on
# Jan 1-3. A period ending this early in January is labelled with the PRIOR
# year, so it cannot collide with the next fiscal year ending in late December.
_EARLY_JANUARY_DAYS = 14

# Balance-sheet items are read at ONE snapshot date. A tag last reported more
# than this many days before the snapshot is treated as no longer outstanding
# (a line the company stopped reporting); one reported within the window (e.g.
# only in the last 10-K while the snapshot is a later 10-Q) is still used.
_INSTANT_GRACE_DAYS = 300

# How many of the most recent fiscal years to retain in the aligned series.
_MAX_YEARS = 8
_MIN_YEARS = 5  # informational target; we keep whatever (>=1) is available

# Tag-fallback lists: try each in order, first present wins.
# `Revenues` is the income-statement total. The ASC 606 tag covers contract
# revenue only (no lease, interest or insurance revenue), so it just backfills
# periods where the total is not tagged.
_TAGS_REVENUE = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
)
_TAGS_EBIT = ("OperatingIncomeLoss",)
# Total costs of sales and operating expenses; EBIT fallback = revenue - this.
_TAGS_COSTS_AND_EXPENSES = ("CostsAndExpenses",)
_TAGS_NET_INCOME = ("NetIncomeLoss", "ProfitLoss")
_TAGS_DA = (
    "DepreciationDepletionAndAmortization",
    "DepreciationAmortizationAndAccretionNet",
    "DepreciationAndAmortization",
    "DepreciationNonproduction",
    # PP&E depreciation only (no amortization): a last resort that still beats
    # a zero-filled year.
    "Depreciation",
)
_TAGS_CAPEX = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
)
_TAGS_INTEREST = (
    "InterestExpense",
    "InterestAndDebtExpense",
    "InterestExpenseNonoperating",
    "InterestExpenseDebt",
    "InterestPaidNet",
    "InterestPaid",
)
_TAGS_TAX = ("IncomeTaxExpenseBenefit",)
_TAGS_PRETAX = (
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
)
_TAGS_DIVIDENDS = (
    "PaymentsOfDividendsCommonStock",
    "PaymentsOfDividends",
    "DividendsCommonStockCash",
)
_TAGS_DILUTED_SHARES = (
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic",
)
_TAGS_NWC = ("IncreaseDecreaseInOperatingCapital",)
# Trade working-capital components from the cash-flow statement, used when the
# aggregate tag is absent. XBRL "IncreaseDecreaseIn<X>" is the change in the
# balance itself: an asset increase is a cash use (+dNWC), a liability increase
# a cash source (-dNWC).
_TAGS_NWC_RECEIVABLES = (
    "IncreaseDecreaseInAccountsReceivable",
    "IncreaseDecreaseInAccountsAndNotesReceivable",
    "IncreaseDecreaseInReceivables",
)
_TAGS_NWC_INVENTORIES = ("IncreaseDecreaseInInventories",)
_TAGS_NWC_PAYABLES = (
    "IncreaseDecreaseInAccountsPayable",
    "IncreaseDecreaseInAccountsPayableTrade",
    "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities",
)

# Instant (balance-sheet) tags.
# Noncurrent long-term debt (LongTermDebtAndCapitalLeaseObligations is also the
# noncurrent line, including lease obligations).
_TAGS_LTD_NONCURRENT = ("LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations")
_TAGS_LTD_CURRENT = ("LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent")
# Short-term borrowings (which by definition include commercial paper; Apple-
# style filers tag only CommercialPaper).
_TAGS_ST_BORROWINGS = ("ShortTermBorrowings", "CommercialPaper")
# DebtCurrent = current maturities of long-term debt + short-term borrowings.
_TAGS_DEBT_CURRENT = ("DebtCurrent",)
# LongTermDebt includes its current maturities.
_TAGS_LTD_TOTAL = ("LongTermDebt",)
_TAGS_DEBT_COMBINED = ("DebtLongtermAndShorttermCombinedAmount",)
_TAGS_CASH = ("CashAndCashEquivalentsAtCarryingValue",)
_TAGS_ST_INVEST = ("ShortTermInvestments", "MarketableSecuritiesCurrent")
_TAGS_EQUITY = (
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
)
_TAGS_MINORITY = ("MinorityInterest",)
_TAGS_PREFERRED = ("PreferredStockValue", "PreferredStockLiquidationPreferenceValue")

# Share counts report under unit "shares", everything else under "USD".
_UNIT_USD = "USD"
_UNIT_SHARES = "shares"

_DAY_SECONDS = 86400.0


def _days_between(start: str, end: str) -> Optional[float]:
    """Number of days between two ISO `YYYY-MM-DD` dates, or None on bad input."""
    try:
        # Parse without pulling datetime's full strptime overhead per call; the
        # SEC dates are always strict ISO calendar dates.
        sy, sm, sd = (int(p) for p in start.split("-"))
        ey, em, ed = (int(p) for p in end.split("-"))
    except (AttributeError, ValueError):
        return None
    # Convert both to a day ordinal via the proleptic Gregorian calendar.
    import datetime

    try:
        d0 = datetime.date(sy, sm, sd)
        d1 = datetime.date(ey, em, ed)
    except ValueError:
        return None
    return (d1 - d0).days


def _fiscal_year_label(end: str) -> Optional[int]:
    """Fiscal-year label for a period ending on ISO date `end`, or None.

    The calendar year of the period end, except that a period ending in the
    first `_EARLY_JANUARY_DAYS` of January (a 52/53-week year ending on the
    weekend nearest Dec 31) takes the prior year.
    """
    try:
        y, m, d = (int(p) for p in str(end)[:10].split("-"))
    except (TypeError, ValueError):
        return None
    if m == 1 and d <= _EARLY_JANUARY_DAYS:
        return y - 1
    return y


def _fy_list(years: list[int]) -> str:
    """Compact 'FY2019-2021, FY2024' rendering of a sorted year list for notes."""
    runs: list[list[int]] = []
    for y in sorted(years):
        if runs and y == runs[-1][-1] + 1:
            runs[-1].append(y)
        else:
            runs.append([y])
    return ", ".join(
        f"FY{r[0]}" if len(r) == 1 else f"FY{r[0]}-{r[-1]}" for r in runs
    )


# --------------------------------------------------------------------------- #
#  Client
# --------------------------------------------------------------------------- #
class EdgarClient:
    """Thin client over the SEC EDGAR XBRL JSON API.

    The ticker->CIK directory is fetched once and cached on the instance, so a
    single client can resolve many tickers cheaply.
    """

    def __init__(self, user_agent: str = config.SEC_USER_AGENT) -> None:
        self.user_agent = user_agent or config.SEC_USER_AGENT
        # Lazily-populated cache: upper-cased ticker -> (padded cik, name).
        self._ticker_map: Optional[dict[str, tuple[str, str]]] = None
        # A pooled session reuses the TCP connection across the directory +
        # facts calls and keeps the required header on every request.
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Accept": "application/json",
                # data.sec.gov is content-negotiated; Host is set by requests.
            }
        )

    # ----------------------------- HTTP ----------------------------------- #
    def _get_json(self, url: str) -> dict:
        """GET `url` as JSON with the mandatory User-Agent, retries and backoff.

        Retries on transient network errors and on HTTP 429/5xx, sleeping with
        an exponential backoff between attempts. Raises `DataError` once the
        retry budget (`config.SEC_MAX_RETRIES`) is exhausted.
        """
        if not self.user_agent:
            raise DataError("Set SEC_USER_AGENT to an application name and contact email before requesting EDGAR data.")
        last_err: Optional[str] = None
        attempts = max(1, int(config.SEC_MAX_RETRIES))
        for attempt in range(attempts):
            try:
                resp = self._session.get(
                    url,
                    timeout=config.SEC_REQUEST_TIMEOUT,
                    headers={"User-Agent": self.user_agent},
                )
            except requests.RequestException as exc:  # network / timeout error
                last_err = f"network error: {exc}"
            else:
                status = resp.status_code
                if status == 200:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        last_err = f"invalid JSON: {exc}"
                        # Malformed body is unlikely to fix itself; stop early.
                        break
                elif status == 404:
                    # Not found is definitive — no point retrying.
                    raise DataError(f"SEC returned 404 (not found) for {url}")
                elif status == 403:
                    # Almost always a missing/blocked User-Agent. Retrying with
                    # the same header rarely helps, but the budget is small.
                    last_err = (
                        "SEC returned 403 (forbidden) — verify the User-Agent "
                        f"header ({self.user_agent!r}) includes contact info"
                    )
                elif status == 429 or 500 <= status < 600:
                    last_err = f"SEC returned HTTP {status}"
                else:
                    last_err = f"SEC returned unexpected HTTP {status}"

            # Backoff before the next attempt (skip the sleep after the last).
            if attempt < attempts - 1:
                backoff = config.HTTP_RETRY_BACKOFF * (2 ** attempt)
                time.sleep(backoff)

        raise DataError(f"failed to GET {url}: {last_err or 'unknown error'}")

    # --------------------------- ticker -> CIK ---------------------------- #
    def _load_ticker_map(self) -> dict[str, tuple[str, str]]:
        """Fetch (once) and cache the ticker -> (cik, name) directory.

        The SEC payload is a dict keyed by an arbitrary index, each value a
        record like {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}.
        """
        if self._ticker_map is not None:
            return self._ticker_map

        raw = self._get_json(_TICKERS_URL)
        mapping: dict[str, tuple[str, str]] = {}
        # The directory is normally a dict-of-records, but guard for a list too.
        records = raw.values() if isinstance(raw, dict) else raw
        for rec in records:
            if not isinstance(rec, dict):
                continue
            ticker = rec.get("ticker")
            cik_raw = rec.get("cik_str")
            if not ticker or cik_raw is None:
                continue
            try:
                cik = str(int(cik_raw)).zfill(10)
            except (TypeError, ValueError):
                continue
            name = rec.get("title") or ticker
            # First occurrence wins; the directory is effectively unique by
            # ticker, so duplicates (rare) keep the earliest listing.
            mapping.setdefault(str(ticker).upper(), (cik, str(name)))

        self._ticker_map = mapping
        return mapping

    def resolve_cik(self, ticker: str) -> tuple[str, str]:
        """Return ``(10-digit zero-padded CIK, company name)`` for `ticker`.

        Raises `DataError` if the ticker is absent from the SEC directory
        (e.g. a non-US issuer with no EDGAR registration).
        """
        if not ticker or not isinstance(ticker, str):
            raise DataError(f"invalid ticker: {ticker!r}")
        key = ticker.strip().upper()
        mapping = self._load_ticker_map()
        hit = mapping.get(key)
        if hit is None:
            # Some tickers carry a class/exchange suffix (e.g. "BRK.B"); the SEC
            # directory uses "BRK-B". Try a dotted->dashed normalization.
            alt = key.replace(".", "-")
            hit = mapping.get(alt)
        if hit is None:
            raise DataError(f"ticker {ticker!r} not found in SEC EDGAR directory")
        return hit

    # --------------------------- company facts ---------------------------- #
    def company_facts(self, cik: str) -> dict:
        """GET the XBRL company-facts document for a zero-padded `cik`."""
        if not cik:
            raise DataError("company_facts called with empty CIK")
        padded = str(cik).strip().zfill(10)
        url = _FACTS_URL.format(cik=padded)
        data = self._get_json(url)
        if not isinstance(data, dict):
            raise DataError(f"unexpected company-facts payload for CIK {padded}")
        return data

    # ------------------------- fact extraction ---------------------------- #
    @staticmethod
    def _unit_entries(facts: dict, tag: str, unit: str) -> list[dict]:
        """Return the list of fact entries for ``us-gaap[tag].units[unit]``.

        Returns an empty list if the tag, the unit, or the path is absent.
        """
        try:
            gaap = facts.get("facts", {}).get("us-gaap", {})
            node = gaap.get(tag)
            if not node:
                return []
            units = node.get("units", {})
            entries = units.get(unit)
            return entries if isinstance(entries, list) else []
        except AttributeError:
            return []

    def _annual_flow_by_fy(
        self, facts: dict, tags: tuple[str, ...], unit: str = _UNIT_USD
    ) -> dict[int, float]:
        """Map fiscal-year label -> value for a FLOW item.

        Keyed by the reporting PERIOD (``end`` date), NOT the XBRL ``fy`` field.
        The same period is reported in several filings -- the original plus
        comparatives in later 10-Ks -- each carrying a *different* ``fy`` (the
        filing's fiscal year, not the period's). Keying by ``fy`` therefore both
        collides the current/prior year onto one key and hides restatements
        (e.g. a switched dividend tag, or the comparative years a post-split
        10-K restates). We instead:

          * keep entries with ``fp == 'FY'`` and ``form`` in the annual forms
            (including amendments), whose period length (``end`` - ``start``)
            is a full year;
          * dedupe by ``end`` date, keeping the LATEST ``filed`` value so
            restatements and amendments win over the original report;
          * BACKFILL across the tag-fallback list: the highest-preference tag
            wins for any period it covers, and lower-preference tags fill the
            periods it doesn't (so a company that changed tags over time still
            gets a complete series);
          * collapse each ``end`` date to a fiscal-year label (the calendar year
            of the period end, or the prior year for a 52/53-week year ending in
            early January), keeping the latest period if two share a label.

        Only periods some later filing re-reported are restated: e.g. share
        counts older than a split's restated comparatives stay unadjusted.
        """
        # end-date -> (filed, value). Later (lower-preference) tags only fill in
        # periods the earlier tags did not already cover.
        by_end: dict[str, tuple[str, float]] = {}
        for tag in tags:
            entries = self._unit_entries(facts, tag, unit)
            if not entries:
                continue
            tag_by_end: dict[str, tuple[str, float]] = {}
            for e in entries:
                if not isinstance(e, dict):
                    continue
                if e.get("fp") != "FY":
                    continue
                if e.get("form") not in _ANNUAL_FORMS:
                    continue
                start = e.get("start")
                end = e.get("end")
                if not start or not end:
                    continue
                span = _days_between(start, end)
                if span is None or not (_MIN_PERIOD_DAYS <= span <= _MAX_PERIOD_DAYS):
                    continue
                val = e.get("val")
                if not is_num(val):
                    continue
                filed = e.get("filed") or ""
                prev = tag_by_end.get(end)
                if prev is None or filed >= prev[0]:
                    tag_by_end[end] = (filed, float(val))
            # Backfill: keep the higher-preference tag's value for shared periods.
            for end, fv in tag_by_end.items():
                by_end.setdefault(end, fv)

        if not by_end:
            return {}

        # Collapse end-date -> fiscal-year label, keeping the latest end date if
        # two periods share a label (which only happens across a fiscal-year-
        # end change).
        by_year: dict[int, tuple[str, float]] = {}
        for end, (filed, val) in by_end.items():
            year = _fiscal_year_label(end)
            if year is None:
                continue
            prev = by_year.get(year)
            if prev is None or end > prev[0]:
                by_year[year] = (end, val)
        return {year: ev[1] for year, ev in by_year.items()}

    def _instant_by_end(
        self, facts: dict, tag: str, unit: str = _UNIT_USD
    ) -> dict[str, float]:
        """``end date -> value`` for an INSTANT tag, the latest filing winning."""
        best: dict[str, tuple[str, float]] = {}
        for e in self._unit_entries(facts, tag, unit):
            if not isinstance(e, dict):
                continue
            end = e.get("end")
            val = e.get("val")
            if not end or not is_num(val):
                continue
            filed = e.get("filed") or ""
            prev = best.get(end)
            if prev is None or filed >= prev[0]:
                best[end] = (filed, float(val))
        return {end: fv[1] for end, fv in best.items()}

    def _instant_at(
        self,
        facts: dict,
        tags: tuple[str, ...],
        as_of: str,
        notes: list[str],
        label: str,
    ) -> Optional[float]:
        """Value of a balance-sheet item at the snapshot date `as_of`, or None.

        The first tag (in preference order) with a fact dated exactly `as_of`
        wins. Failing that, the most recent fact dated within
        `_INSTANT_GRACE_DAYS` before `as_of` is used (noted). An item whose
        latest fact is older than that is treated as no longer reported: None,
        with a note when the ignored value was non-zero. Facts dated after
        `as_of` are never used.
        """
        series = [self._instant_by_end(facts, tag) for tag in tags]
        for by_end in series:
            if as_of in by_end:
                return by_end[as_of]
        older: list[tuple[str, int, float]] = []  # (end, -preference, value)
        for pref, by_end in enumerate(series):
            for end, val in by_end.items():
                if end < as_of:
                    older.append((end, -pref, val))
        if not older:
            return None
        end, _, val = max(older)
        age = _days_between(end, as_of)
        if age is not None and age <= _INSTANT_GRACE_DAYS:
            notes.append(f"{label} taken from {end}; not reported at the {as_of} balance sheet")
            return val
        if val != 0.0:
            notes.append(
                f"{label} last reported {end} ({val:,.0f}); treated as 0 at the "
                f"{as_of} balance sheet"
            )
        return None

    # ------------------------ public entry point -------------------------- #
    def get_annual_financials(
        self, ticker: str
    ) -> tuple[AnnualFinancials, BalanceSheetSnapshot, str, str]:
        """Build normalized fundamentals for `ticker` from SEC EDGAR.

        Returns ``(AnnualFinancials, BalanceSheetSnapshot, cik, company_name)``.
        Raises `DataError` if the ticker can't be resolved, if both revenue
        and net income are entirely unavailable, or if one of them ends more
        than a fiscal year before the other (a tag switch we can't follow would
        otherwise value the company on years-old statements).
        """
        cik, name = self.resolve_cik(ticker)
        facts = self.company_facts(cik)
        notes: list[str] = []
        # Informational notes (expected derivations) go after the data gaps so
        # the material ones lead the list the UI and exports show.
        info_notes: list[str] = []

        # ---- pull each FLOW item as fy -> value -------------------------- #
        revenue_by_fy = self._annual_flow_by_fy(facts, _TAGS_REVENUE)
        ebit_by_fy = self._annual_flow_by_fy(facts, _TAGS_EBIT)
        ni_by_fy = self._annual_flow_by_fy(facts, _TAGS_NET_INCOME)
        da_by_fy = self._annual_flow_by_fy(facts, _TAGS_DA)
        capex_by_fy = self._annual_flow_by_fy(facts, _TAGS_CAPEX)
        interest_by_fy = self._annual_flow_by_fy(facts, _TAGS_INTEREST)
        tax_by_fy = self._annual_flow_by_fy(facts, _TAGS_TAX)
        pretax_by_fy = self._annual_flow_by_fy(facts, _TAGS_PRETAX)
        div_by_fy = self._annual_flow_by_fy(facts, _TAGS_DIVIDENDS)
        shares_by_fy = self._annual_flow_by_fy(facts, _TAGS_DILUTED_SHARES, _UNIT_SHARES)
        nwc_by_fy = self._annual_flow_by_fy(facts, _TAGS_NWC)

        # ---- fatal guard: need revenue OR net income --------------------- #
        if not revenue_by_fy and not ni_by_fy:
            raise DataError(
                f"no annual revenue or net income available on EDGAR for "
                f"{ticker!r} (CIK {cik}); not usable for valuation"
            )

        # ---- staleness guard: the essentials must end in the same year ---- #
        # If a filer moved revenue (or net income) to a tag we don't read, the
        # intersection below would silently end years in the past.
        if revenue_by_fy and ni_by_fy:
            rev_last, ni_last = max(revenue_by_fy), max(ni_by_fy)
            if abs(rev_last - ni_last) > 1:
                raise DataError(
                    f"EDGAR revenue runs to FY{rev_last} but net income to "
                    f"FY{ni_last} for {ticker!r} (CIK {cik}); the lagging series "
                    "moved to a tag this parser does not read, so the EDGAR "
                    "history is stale"
                )
            if rev_last != ni_last:
                lagging = "revenue" if rev_last < ni_last else "net income"
                notes.append(
                    f"{lagging} on EDGAR ends FY{min(rev_last, ni_last)} while the "
                    f"other runs to FY{max(rev_last, ni_last)}; the latest fiscal "
                    "year is left out of the aligned history"
                )

        # ---- choose the aligned fiscal-year axis ------------------------- #
        # Anchor on whichever of the two essential series is present; intersect
        # with the other essential series when both exist so every retained year
        # has at least revenue and net income.
        if revenue_by_fy and ni_by_fy:
            year_set = set(revenue_by_fy) & set(ni_by_fy)
            if not year_set:
                # No overlap: fall back to the union of the essentials and note
                # the gaps (each missing essential is filled below).
                year_set = set(revenue_by_fy) | set(ni_by_fy)
                notes.append(
                    "revenue and net income reported for disjoint fiscal years; "
                    "missing essentials filled with 0.0"
                )
        elif revenue_by_fy:
            year_set = set(revenue_by_fy)
            notes.append("net income unavailable on EDGAR; filled with 0.0")
        else:
            year_set = set(ni_by_fy)
            notes.append("revenue unavailable on EDGAR; filled with 0.0")

        # Keep the most recent _MAX_YEARS, ordered oldest -> newest.
        years = sorted(year_set)[-_MAX_YEARS:]
        if not years:
            # Defensive: the essentials existed but produced no usable fy axis.
            raise DataError(
                f"could not align any fiscal year for {ticker!r} (CIK {cik})"
            )
        if len(years) < _MIN_YEARS:
            notes.append(
                f"only {len(years)} annual period(s) available on EDGAR "
                f"(target is {_MIN_YEARS}-{_MAX_YEARS})"
            )

        # ---- helper to align one flow series onto `years` ---------------- #
        def _align(
            by_fy: dict[int, float],
            *,
            positive: bool = False,
            label: str = "",
            note_if_empty: bool = False,
            gap_notes: Optional[list[str]] = None,
        ) -> list[float]:
            """Project `by_fy` onto `years`, filling gaps with 0.0.

            `positive=True` stores the absolute magnitude (capex, D&A, etc. are
            signed differently across filers; the schemas want +X).
            With `note_if_empty`, a wholly missing series and any individual
            missing years are both noted (in `gap_notes`, default `notes`).
            """
            out: list[float] = []
            missing: list[int] = []
            for y in years:
                v = by_fy.get(y)
                if v is None or not is_num(v):
                    out.append(0.0)
                    missing.append(y)
                else:
                    out.append(abs(v) if positive else float(v))
            if note_if_empty:
                if not by_fy:
                    notes.append(f"{label} unavailable on EDGAR; filled with 0.0")
                elif missing:
                    (notes if gap_notes is None else gap_notes).append(
                        f"{label} not reported on EDGAR for {_fy_list(missing)}; "
                        "filled with 0.0"
                    )
            return out

        # EBIT: OperatingIncomeLoss where tagged. Filers without an operating-
        # income subtotal (many pharma, banks, insurers) get it derived per year
        # instead of a silent 0.0: pretax income + interest expense (the
        # textbook EBIT, both already parsed), else revenue - CostsAndExpenses.
        ebit_by_fy = dict(ebit_by_fy)
        costs_by_fy: Optional[dict[int, float]] = None
        from_pretax: list[int] = []
        from_costs: list[int] = []
        for y in years:
            if ebit_by_fy.get(y) is not None:
                continue
            pti = pretax_by_fy.get(y)
            if pti is not None:
                ebit_by_fy[y] = pti + abs(interest_by_fy.get(y, 0.0))
                from_pretax.append(y)
                continue
            if costs_by_fy is None:
                costs_by_fy = self._annual_flow_by_fy(facts, _TAGS_COSTS_AND_EXPENSES)
            rev, costs = revenue_by_fy.get(y), costs_by_fy.get(y)
            if rev is not None and costs is not None:
                ebit_by_fy[y] = rev - abs(costs)
                from_costs.append(y)
        if from_pretax:
            notes.append(
                f"EBIT (OperatingIncomeLoss) not reported on EDGAR for "
                f"{_fy_list(from_pretax)}; derived as pretax income + interest expense"
            )
        if from_costs:
            notes.append(
                f"EBIT (OperatingIncomeLoss) not reported on EDGAR for "
                f"{_fy_list(from_costs)}; derived as revenue - CostsAndExpenses"
            )

        revenue = _align(revenue_by_fy, label="revenue")
        ebit = _align(ebit_by_fy, label="EBIT (operating income)", note_if_empty=True)
        net_income = _align(ni_by_fy, label="net income")
        dep_amort = _align(da_by_fy, positive=True, label="D&A", note_if_empty=True)
        capex = _align(capex_by_fy, positive=True, label="capex", note_if_empty=True)
        interest_expense = _align(
            interest_by_fy, positive=True, label="interest expense",
            note_if_empty=True, gap_notes=info_notes,
        )
        # Signed: IncomeTaxExpenseBenefit is negative for a tax benefit, and the
        # effective-tax-rate median needs that sign.
        tax_expense = _align(tax_by_fy, label="tax expense", note_if_empty=True)
        pretax_income = _align(
            pretax_by_fy, label="pretax income", note_if_empty=True
        )
        dividends_paid = _align(
            div_by_fy, positive=True, label="dividends paid",
            note_if_empty=True, gap_notes=info_notes,
        )
        diluted_shares = _align(
            shares_by_fy, label="diluted shares", note_if_empty=True
        )

        # ΔNWC (positive = increase = cash use). Prefer the aggregate operating-
        # capital tag; most filers only tag the components, so fall back to the
        # trade working capital change: receivables + inventories - payables.
        if nwc_by_fy:
            change_in_nwc = _align(nwc_by_fy, label="change in NWC")
        else:
            ar = self._annual_flow_by_fy(facts, _TAGS_NWC_RECEIVABLES)
            inv = self._annual_flow_by_fy(facts, _TAGS_NWC_INVENTORIES)
            ap = self._annual_flow_by_fy(facts, _TAGS_NWC_PAYABLES)
            if ar or inv or ap:
                change_in_nwc = [
                    ar.get(y, 0.0) + inv.get(y, 0.0) - ap.get(y, 0.0) for y in years
                ]
                info_notes.append(
                    "change in net working capital not tagged on EDGAR; derived "
                    "from the receivables, inventories and payables changes on "
                    "the cash-flow statement"
                )
            else:
                change_in_nwc = [0.0 for _ in years]
                info_notes.append(
                    "change in net working capital not reported on EDGAR; left "
                    "as 0.0, so the derived working-capital drag is zero unless "
                    "nwc_pct_revenue is set"
                )

        # EBITDA is computed, never looked up: EBITDA = EBIT + D&A per year.
        ebitda = [e + d for e, d in zip(ebit, dep_amort)]

        financials = AnnualFinancials(
            fiscal_years=list(years),
            revenue=revenue,
            ebit=ebit,
            ebitda=ebitda,
            net_income=net_income,
            dep_amort=dep_amort,
            capex=capex,
            change_in_nwc=change_in_nwc,
            interest_expense=interest_expense,
            tax_expense=tax_expense,
            pretax_income=pretax_income,
            dividends_paid=dividends_paid,
            diluted_shares=diluted_shares,
        )

        # ---- balance sheet (INSTANT facts at one snapshot date) ----------- #
        balance_sheet = self._build_balance_sheet(facts, notes, info_notes)

        # The schemas have no notes field on these dataclasses, so the parsing
        # notes ride on the financials object as a dynamic attribute that
        # HybridProvider copies into CompanyData.source_notes (data gaps first,
        # informational derivations last).
        try:
            setattr(financials, "_source_notes", notes + info_notes)
        except Exception:  # pragma: no cover - dataclasses allow attr set
            pass

        return financials, balance_sheet, cik, name

    # --------------------------- balance sheet ---------------------------- #
    def _build_balance_sheet(
        self, facts: dict, notes: list[str], info_notes: Optional[list[str]] = None
    ) -> BalanceSheetSnapshot:
        """Assemble the latest `BalanceSheetSnapshot` from instant facts.

        Every item is read at ONE snapshot date (the latest date at which equity
        or cash is reported), so a line the company stopped reporting years ago
        is not summed into today's balance sheet (see `_instant_at`). Missing
        essentials are noted in `notes`; date adjustments in `info_notes`.
        """
        date_notes = info_notes if info_notes is not None else notes

        def latest_end(tag_groups: tuple[tuple[str, ...], ...]) -> Optional[str]:
            ends = [
                end
                for tags in tag_groups
                for tag in tags
                for end in self._instant_by_end(facts, tag)
            ]
            return max(ends) if ends else None

        as_of = latest_end((_TAGS_EQUITY, _TAGS_CASH)) or latest_end((
            _TAGS_LTD_NONCURRENT, _TAGS_LTD_CURRENT, _TAGS_ST_BORROWINGS,
            _TAGS_DEBT_CURRENT, _TAGS_LTD_TOTAL, _TAGS_DEBT_COMBINED,
            _TAGS_ST_INVEST, _TAGS_MINORITY, _TAGS_PREFERRED,
        ))
        if as_of is None:
            notes.append("no balance-sheet facts on EDGAR; debt, cash and equity set to 0.0")
            return BalanceSheetSnapshot(
                as_of="", total_debt=0.0, cash_and_investments=0.0, total_equity=0.0,
            )

        def at(tags: tuple[str, ...], label: str) -> Optional[float]:
            return self._instant_at(facts, tags, as_of, date_notes, label)

        # Total debt = noncurrent LTD + current debt, where current debt is
        # DebtCurrent if tagged (it already includes the current maturities of
        # LTD and short-term borrowings, so never add those to it) or else the
        # sum of those components. Without a noncurrent tag, LongTermDebt (which
        # includes its current maturities) plus short-term borrowings, then the
        # combined debt tag.
        ltd_nc = at(_TAGS_LTD_NONCURRENT, "long-term debt (noncurrent)")
        ltd_cur = at(_TAGS_LTD_CURRENT, "current portion of long-term debt")
        st_borrow = at(_TAGS_ST_BORROWINGS, "short-term borrowings")
        debt_cur = at(_TAGS_DEBT_CURRENT, "current debt")
        if debt_cur is not None:
            current_debt: Optional[float] = debt_cur
        elif ltd_cur is not None or st_borrow is not None:
            current_debt = (ltd_cur or 0.0) + (st_borrow or 0.0)
        else:
            current_debt = None

        if ltd_nc is not None:
            total_debt = ltd_nc + (current_debt or 0.0)
        else:
            ltd_total = at(_TAGS_LTD_TOTAL, "long-term debt")
            combined = None if ltd_total is not None else at(
                _TAGS_DEBT_COMBINED, "total debt")
            if ltd_total is not None:
                if st_borrow is not None:
                    short_only = st_borrow
                elif debt_cur is not None and ltd_cur is not None:
                    short_only = max(debt_cur - ltd_cur, 0.0)
                else:
                    short_only = 0.0
                total_debt = ltd_total + short_only
            elif combined is not None:
                total_debt = combined
            elif current_debt is not None:
                total_debt = current_debt
                notes.append(
                    "long-term debt not reported on EDGAR; total debt counts "
                    "current debt only"
                )
            else:
                total_debt = 0.0
                notes.append("total debt unavailable on EDGAR; set to 0.0")

        # Cash & short-term investments.
        cash = at(_TAGS_CASH, "cash & equivalents")
        if cash is None:
            notes.append("cash & equivalents unavailable on EDGAR; set to 0.0")
        st_inv = at(_TAGS_ST_INVEST, "short-term investments")
        cash_and_investments = (cash or 0.0) + (st_inv or 0.0)

        # Total common equity (book value).
        equity = at(_TAGS_EQUITY, "stockholders' equity")
        if equity is None:
            notes.append("total equity unavailable on EDGAR; set to 0.0")

        # Minority (noncontrolling) interest and preferred stock -- optional,
        # default 0; both are claims the equity bridges subtract from EV.
        minority = at(_TAGS_MINORITY, "minority interest")
        preferred = at(_TAGS_PREFERRED, "preferred stock")

        return BalanceSheetSnapshot(
            as_of=as_of,
            total_debt=float(total_debt),
            cash_and_investments=float(cash_and_investments),
            total_equity=float(equity or 0.0),
            minority_interest=float(minority or 0.0),
            preferred_equity=float(preferred or 0.0),
        )
