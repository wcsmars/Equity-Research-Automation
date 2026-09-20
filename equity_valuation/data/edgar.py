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
  * `capex`, `dep_amort`, `interest_expense`, `tax_expense`, `dividends_paid` are
    stored as POSITIVE magnitudes regardless of XBRL sign.
  * Annual series are ordered OLDEST -> NEWEST; the last element is most recent
    and aligns with the `BalanceSheetSnapshot`.

Only `revenue` or `net_income` being entirely unavailable is fatal (raises
`DataError`); every other gap degrades gracefully (zeros + a source note).
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

# Annual filing forms we accept for FLOW (income/cash-flow) frames.
_ANNUAL_FORMS = ("10-K", "20-F")

# A "full-year" period: end - start measured in days. We allow a generous band
# to absorb 52/53-week fiscal calendars and minor reporting drift.
_MIN_PERIOD_DAYS = 330
_MAX_PERIOD_DAYS = 400

# How many of the most recent fiscal years to retain in the aligned series.
_MAX_YEARS = 8
_MIN_YEARS = 5  # informational target; we keep whatever (>=1) is available

# Tag-fallback lists: try each in order, first present wins.
_TAGS_REVENUE = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
)
_TAGS_EBIT = ("OperatingIncomeLoss",)
_TAGS_NET_INCOME = ("NetIncomeLoss", "ProfitLoss")
_TAGS_DA = (
    "DepreciationDepletionAndAmortization",
    "DepreciationAmortizationAndAccretionNet",
    "DepreciationAndAmortization",
    "DepreciationNonproduction",
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

# Instant (balance-sheet) tags.
_TAGS_LTD_NONCURRENT = ("LongTermDebtNoncurrent",)
_TAGS_LTD_CURRENT = ("LongTermDebtCurrent",)
_TAGS_ST_DEBT = ("ShortTermBorrowings", "DebtCurrent")
_TAGS_TOTAL_DEBT_FALLBACK = (
    "LongTermDebt",
    "DebtLongtermAndShorttermCombinedAmount",
)
_TAGS_CASH = ("CashAndCashEquivalentsAtCarryingValue",)
_TAGS_ST_INVEST = ("ShortTermInvestments", "MarketableSecuritiesCurrent")
_TAGS_EQUITY = (
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
)
_TAGS_MINORITY = ("MinorityInterest",)

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
        (e.g. Apple's pre-/post-4:1-split share counts, or a switched dividend
        tag). We instead:

          * keep entries with ``fp == 'FY'`` and ``form`` in the annual forms,
            whose period length (``end`` - ``start``) is a full year;
          * dedupe by ``end`` date, keeping the LATEST ``filed`` value so
            restatements win over the original report;
          * BACKFILL across the tag-fallback list: the highest-preference tag
            wins for any period it covers, and lower-preference tags fill the
            periods it doesn't (so a company that changed tags over time still
            gets a complete series);
          * collapse each ``end`` date to a fiscal-year label (the calendar year
            of the period end), keeping the latest period if two share a label.
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

        # Collapse end-date -> fiscal-year label (calendar year of the period
        # end), keeping the latest end date if two periods share a label (which
        # only happens across a fiscal-year-end change).
        by_year: dict[int, tuple[str, float]] = {}
        for end, (filed, val) in by_end.items():
            try:
                year = int(str(end)[:4])
            except (TypeError, ValueError):
                continue
            prev = by_year.get(year)
            if prev is None or end > prev[0]:
                by_year[year] = (end, val)
        return {year: ev[1] for year, ev in by_year.items()}

    def _instant_latest(
        self, facts: dict, tags: tuple[str, ...], unit: str = _UNIT_USD
    ) -> tuple[Optional[float], Optional[str]]:
        """Return ``(value, end_date)`` for the most recent INSTANT fact.

        Uses the first tag that has any entries; among those, the one with the
        latest ``end`` date (ties broken by the latest ``filed`` date).
        """
        for tag in tags:
            entries = self._unit_entries(facts, tag, unit)
            if not entries:
                continue
            best_end = ""
            best_filed = ""
            best_val: Optional[float] = None
            for e in entries:
                if not isinstance(e, dict):
                    continue
                # Balance-sheet items are point-in-time: no 'start'.
                end = e.get("end")
                val = e.get("val")
                if not end or not is_num(val):
                    continue
                filed = e.get("filed") or ""
                if end > best_end or (end == best_end and filed >= best_filed):
                    best_end, best_filed, best_val = end, filed, float(val)
            if best_val is not None:
                return best_val, best_end
        return None, None

    # ------------------------ public entry point -------------------------- #
    def get_annual_financials(
        self, ticker: str
    ) -> tuple[AnnualFinancials, BalanceSheetSnapshot, str, str]:
        """Build normalized fundamentals for `ticker` from SEC EDGAR.

        Returns ``(AnnualFinancials, BalanceSheetSnapshot, cik, company_name)``.
        Raises `DataError` if the ticker can't be resolved or if both revenue
        and net income are entirely unavailable.
        """
        cik, name = self.resolve_cik(ticker)
        facts = self.company_facts(cik)
        notes: list[str] = []

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
        ) -> list[float]:
            """Project `by_fy` onto `years`, filling gaps with 0.0.

            `positive=True` stores the absolute magnitude (capex, D&A, etc. are
            signed differently across filers; the schemas want +X).
            """
            if note_if_empty and not by_fy:
                notes.append(f"{label} unavailable on EDGAR; filled with 0.0")
            out: list[float] = []
            for y in years:
                v = by_fy.get(y)
                if v is None or not is_num(v):
                    out.append(0.0)
                else:
                    out.append(abs(v) if positive else float(v))
            return out

        revenue = _align(revenue_by_fy, label="revenue")
        ebit = _align(ebit_by_fy, label="EBIT (operating income)", note_if_empty=True)
        net_income = _align(ni_by_fy, label="net income")
        dep_amort = _align(da_by_fy, positive=True, label="D&A", note_if_empty=True)
        capex = _align(capex_by_fy, positive=True, label="capex", note_if_empty=True)
        interest_expense = _align(
            interest_by_fy, positive=True, label="interest expense", note_if_empty=True
        )
        tax_expense = _align(
            tax_by_fy, positive=True, label="tax expense", note_if_empty=True
        )
        pretax_income = _align(
            pretax_by_fy, label="pretax income", note_if_empty=True
        )
        dividends_paid = _align(
            div_by_fy, positive=True, label="dividends paid", note_if_empty=True
        )
        diluted_shares = _align(
            shares_by_fy, label="diluted shares", note_if_empty=True
        )

        # ΔNWC: present only when the filer reports the operating-capital change.
        # Otherwise leave zeros — the DCF derives ΔNWC from nwc_pct_revenue.
        if nwc_by_fy:
            change_in_nwc = _align(nwc_by_fy, label="change in NWC")
        else:
            change_in_nwc = [0.0 for _ in years]
            notes.append(
                "change in net working capital not reported on EDGAR; left as "
                "0.0 (DCF derives it from nwc_pct_revenue)"
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

        # ---- balance sheet (most-recent INSTANT facts) ------------------- #
        balance_sheet = self._build_balance_sheet(facts, notes)

        # Surface accumulated parsing issues onto the snapshot's neighbors. The
        # schemas have no notes field on these dataclasses, so the human-readable
        # issues are folded into the BS `as_of` companion via the hybrid
        # provider's source_notes; here we attach them where we can (nowhere on
        # the dataclasses), so they are simply discarded if unused. To avoid
        # losing them entirely we stash them on the financials object as a
        # dynamic attribute the hybrid provider can read.
        try:
            setattr(financials, "_source_notes", notes)
        except Exception:  # pragma: no cover - dataclasses allow attr set
            pass

        return financials, balance_sheet, cik, name

    # --------------------------- balance sheet ---------------------------- #
    def _build_balance_sheet(
        self, facts: dict, notes: list[str]
    ) -> BalanceSheetSnapshot:
        """Assemble the most-recent `BalanceSheetSnapshot` from instant facts."""
        # Total debt: prefer the component sum (LT noncurrent + LT current +
        # short-term), falling back to a combined tag when components are absent.
        ltd_nc, end_nc = self._instant_latest(facts, _TAGS_LTD_NONCURRENT)
        ltd_c, end_c = self._instant_latest(facts, _TAGS_LTD_CURRENT)
        st_debt, end_st = self._instant_latest(facts, _TAGS_ST_DEBT)

        components = [v for v in (ltd_nc, ltd_c, st_debt) if is_num(v)]
        if components:
            total_debt = float(sum(components))
            debt_end_candidates = [e for e in (end_nc, end_c, end_st) if e]
            debt_as_of = max(debt_end_candidates) if debt_end_candidates else None
        else:
            tdebt, tdebt_end = self._instant_latest(facts, _TAGS_TOTAL_DEBT_FALLBACK)
            if is_num(tdebt):
                total_debt = float(tdebt)
                debt_as_of = tdebt_end
            else:
                total_debt = 0.0
                debt_as_of = None
                notes.append("total debt unavailable on EDGAR; set to 0.0")

        # Cash & short-term investments.
        cash, end_cash = self._instant_latest(facts, _TAGS_CASH)
        st_inv, end_inv = self._instant_latest(facts, _TAGS_ST_INVEST)
        cash_val = float(cash) if is_num(cash) else 0.0
        if not is_num(cash):
            notes.append("cash & equivalents unavailable on EDGAR; set to 0.0")
        st_inv_val = float(st_inv) if is_num(st_inv) else 0.0
        cash_and_investments = cash_val + st_inv_val

        # Total common equity (book value).
        equity, end_eq = self._instant_latest(facts, _TAGS_EQUITY)
        total_equity = float(equity) if is_num(equity) else 0.0
        if not is_num(equity):
            notes.append("total equity unavailable on EDGAR; set to 0.0")

        # Minority (noncontrolling) interest — optional, defaults to 0.
        minority, end_mi = self._instant_latest(facts, _TAGS_MINORITY)
        minority_interest = float(minority) if is_num(minority) else 0.0

        # `as_of` is the freshest end-date we saw across the instant items so the
        # snapshot aligns with the most recent reporting period.
        end_dates = [
            d
            for d in (debt_as_of, end_cash, end_inv, end_eq, end_mi)
            if d
        ]
        as_of = max(end_dates) if end_dates else ""

        return BalanceSheetSnapshot(
            as_of=as_of,
            total_debt=total_debt,
            cash_and_investments=cash_and_investments,
            total_equity=total_equity,
            minority_interest=minority_interest,
            preferred_equity=0.0,
        )
