"""Self-contained interactive HTML report exporter.

Renders a :class:`~equity_valuation.schemas.ValuationReport` to a single, offline
HTML file using plotly for the interactive charts. The plotly JavaScript library
is embedded ONCE (on the first figure with ``include_plotlyjs=True``) and every
subsequent figure is serialized with ``include_plotlyjs=False`` so the document
stays self-contained but does not bloat with repeated copies of the library.

Design notes:
  * Every model on the report is optional. We guard every access and simply omit
    a section (and record nothing fatal) when its model is ``None`` or its inputs
    are unusable. The exporter must never crash on a partially-filled report.
  * Money is in absolute reporting-currency units (see schemas.py); we format with
    thousands separators and a currency symbol pulled from
    ``config.CURRENCY_SYMBOLS``. Large magnitudes are abbreviated (K/M/B/T) so the
    tables stay readable.
  * Rates are decimals; we render them as percentages.

"""

from __future__ import annotations

import datetime
import html as _html
from typing import Optional

from .. import config
from ..schemas import ValuationReport
from ..utils import is_num, median

# plotly is the only hard third-party dependency of this module. Import it at the
# top so an environment without plotly fails loudly rather than silently writing
# a broken file.
import plotly.graph_objects as go


# --------------------------------------------------------------------------- #
#  Number / string formatting helpers
# --------------------------------------------------------------------------- #
def _currency_symbol(currency: Optional[str]) -> str:
    """Currency glyph for a reporting currency, '' when unknown/None."""
    if not currency:
        return ""
    return config.CURRENCY_SYMBOLS.get(currency, "")


def _fmt_price(value: Optional[float], symbol: str = "") -> str:
    """Per-share price: 2 decimals with thousands separators, e.g. '$1,234.50'."""
    if not is_num(value):
        return "n/a"
    return f"{symbol}{value:,.2f}"


def _fmt_big(value: Optional[float], symbol: str = "") -> str:
    """Abbreviate a large absolute amount (K/M/B/T) for compact table cells."""
    if not is_num(value):
        return "n/a"
    sign = "-" if value < 0 else ""
    a = abs(float(value))
    for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= threshold:
            return f"{sign}{symbol}{a / threshold:,.2f}{suffix}"
    return f"{sign}{symbol}{a:,.0f}"


def _fmt_pct(value: Optional[float], decimals: int = 1) -> str:
    """Decimal rate -> percent string (0.082 -> '8.2%')."""
    if not is_num(value):
        return "n/a"
    return f"{value * 100:.{decimals}f}%"


def _fmt_mult(value: Optional[float]) -> str:
    """Trading multiple, e.g. '12.3x'."""
    if not is_num(value):
        return "n/a"
    return f"{value:,.1f}x"


def _fmt_num(value: Optional[float], decimals: int = 2) -> str:
    """Plain number with thousands separators."""
    if not is_num(value):
        return "n/a"
    return f"{value:,.{decimals}f}"


def _esc(text: object) -> str:
    """HTML-escape any value coerced to str (None -> '')."""
    if text is None:
        return ""
    return _html.escape(str(text))


def _upside_class(upside: Optional[float]) -> str:
    """CSS class name encoding the sign of an upside figure."""
    if not is_num(upside):
        return "neutral"
    return "pos" if upside >= 0 else "neg"


# --------------------------------------------------------------------------- #
#  Figure builders (each returns an HTML fragment or '' when not applicable)
# --------------------------------------------------------------------------- #
# We track whether plotly.js has already been embedded so only the FIRST figure
# carries the library. ``_PlotEmbedder`` keeps that state local to one render:
# write_html creates one per call and hands it to every figure builder, so
# concurrent renders (the backend exports from request threads) never share it.
class _PlotEmbedder:
    """Serializes plotly figures, embedding plotly.js exactly once."""

    def __init__(self) -> None:
        self._embedded = False

    def to_html(self, fig: "go.Figure") -> str:
        include = not self._embedded
        # First figure carries the full library (offline render); the rest skip it.
        frag = fig.to_html(
            full_html=False,
            include_plotlyjs=True if include else False,
            config={"displayModeBar": False, "responsive": True},
        )
        self._embedded = True
        return frag


def _football_field_fig(report: ValuationReport, symbol: str,
                        embed: _PlotEmbedder) -> str:
    """Horizontal floating bars (low->high) with a base marker and a current-price line."""
    rows = [r for r in (report.football_field or []) if r is not None]
    # Keep only rows with at least a usable low/high span.
    rows = [r for r in rows if is_num(getattr(r, "low", None)) and is_num(getattr(r, "high", None))]
    if not rows:
        return ""

    methods = [_esc(r.method) for r in rows]
    lows = [float(r.low) for r in rows]
    highs = [float(r.high) for r in rows]
    bases = [float(r.base) if is_num(getattr(r, "base", None)) else None for r in rows]
    # Span (width) of each floating bar starts at `low` (the `base` offset).
    spans = [hi - lo for lo, hi in zip(lows, highs)]

    fig = go.Figure()
    # Invisible base segment positions each visible bar to start at `low`.
    fig.add_trace(
        go.Bar(
            y=methods,
            x=lows,
            orientation="h",
            marker=dict(color="rgba(0,0,0,0)"),
            hoverinfo="skip",
            showlegend=False,
        )
    )
    fig.add_trace(
        go.Bar(
            y=methods,
            x=spans,
            base=lows,
            orientation="h",
            marker=dict(color="#5b8def", line=dict(color="#3a66c4", width=1)),
            name="Range",
            customdata=list(zip(lows, highs)),
            hovertemplate=(
                "%{y}<br>Low: " + symbol + "%{customdata[0]:,.2f}"
                "<br>High: " + symbol + "%{customdata[1]:,.2f}<extra></extra>"
            ),
            showlegend=False,
        )
    )
    # Base (central estimate) markers, where present.
    base_methods = [m for m, b in zip(methods, bases) if b is not None]
    base_vals = [b for b in bases if b is not None]
    if base_vals:
        fig.add_trace(
            go.Scatter(
                y=base_methods,
                x=base_vals,
                mode="markers",
                marker=dict(symbol="diamond", size=11, color="#16324f",
                            line=dict(color="white", width=1)),
                name="Base case",
                hovertemplate="%{y}<br>Base: " + symbol + "%{x:,.2f}<extra></extra>",
            )
        )

    # Vertical reference line at the current price.
    cur = report.current_price
    if is_num(cur):
        fig.add_vline(
            x=float(cur),
            line=dict(color="#c0392b", width=2, dash="dash"),
            annotation_text="Current " + _fmt_price(cur, symbol),
            annotation_position="top",
        )

    fig.update_layout(
        barmode="stack",
        title="Valuation summary (football field)",
        xaxis_title="Implied price per share (" + (symbol or "") + ")",
        height=max(260, 70 * len(rows) + 120),
        margin=dict(l=10, r=20, t=60, b=40),
        plot_bgcolor="white",
        paper_bgcolor="white",
        font=dict(family="Segoe UI, Helvetica, Arial, sans-serif", size=13),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eee", zeroline=False)
    return embed.to_html(fig)


def _fcff_fig(report: ValuationReport, symbol: str, embed: _PlotEmbedder) -> str:
    """Bar chart of projected FCFF by forecast year."""
    dcf = report.dcf
    if dcf is None:
        return ""
    years = list(getattr(dcf, "years", []) or [])
    fcff = list(getattr(dcf, "fcff", []) or [])
    pairs = [(y, f) for y, f in zip(years, fcff) if is_num(f)]
    if not pairs:
        return ""
    xs = [str(y) for y, _ in pairs]
    ys = [f for _, f in pairs]
    fig = go.Figure(
        go.Bar(
            x=xs,
            y=ys,
            marker=dict(color="#2e8b8b"),
            hovertemplate="Year %{x}<br>FCFF: " + symbol + "%{y:,.0f}<extra></extra>",
        )
    )
    fig.update_layout(
        title="DCF — projected unlevered free cash flow (FCFF)",
        xaxis_title="Forecast year",
        yaxis_title="FCFF (" + (symbol or "") + ")",
        height=340,
        margin=dict(l=10, r=20, t=60, b=40),
        plot_bgcolor="white",
        paper_bgcolor="white",
        font=dict(family="Segoe UI, Helvetica, Arial, sans-serif", size=13),
    )
    fig.update_yaxes(showgrid=True, gridcolor="#eee", zeroline=True, zerolinecolor="#ccc")
    return embed.to_html(fig)


def _comps_fig(report: ValuationReport, embed: _PlotEmbedder) -> str:
    """Bar chart of each peer's EV/EBITDA against the target's EV/EBITDA."""
    comps = report.comps
    if comps is None:
        return ""
    peers = [p for p in (getattr(comps, "peers", []) or [])
             if p is not None and is_num(getattr(p, "ev_ebitda", None))]
    target = getattr(comps, "target", None)
    target_mult = getattr(target, "ev_ebitda", None) if target is not None else None
    if not peers and not is_num(target_mult):
        return ""

    labels = [_esc(getattr(p, "ticker", None) or getattr(p, "name", "?")) for p in peers]
    values = [float(p.ev_ebitda) for p in peers]
    colors = ["#5b8def"] * len(peers)

    # Append the target as a highlighted bar.
    if is_num(target_mult):
        labels.append(_esc(getattr(target, "ticker", None) or "Target"))
        values.append(float(target_mult))
        colors.append("#c0392b")

    # Peer median reference line: the comps model's (outlier-trimmed) median,
    # which is what the stats table and the implied prices use.
    med = ((getattr(comps, "stats", None) or {}).get("ev_ebitda") or {}).get("median")
    if not is_num(med):
        med = median(values[:len(peers)]) if peers else None

    fig = go.Figure(
        go.Bar(
            x=labels,
            y=values,
            marker=dict(color=colors),
            hovertemplate="%{x}<br>EV/EBITDA: %{y:.1f}x<extra></extra>",
        )
    )
    if is_num(med):
        fig.add_hline(
            y=float(med),
            line=dict(color="#16324f", width=1.5, dash="dot"),
            annotation_text="Peer median " + _fmt_mult(med),
            annotation_position="top left",
        )
    fig.update_layout(
        title="Comps — EV/EBITDA: peers vs target",
        xaxis_title="",
        yaxis_title="EV / EBITDA (x)",
        height=360,
        margin=dict(l=10, r=20, t=60, b=60),
        plot_bgcolor="white",
        paper_bgcolor="white",
        font=dict(family="Segoe UI, Helvetica, Arial, sans-serif", size=13),
    )
    fig.update_yaxes(showgrid=True, gridcolor="#eee", zeroline=False)
    return embed.to_html(fig)


def _sensitivity_figs(report: ValuationReport, symbol: str,
                      embed: _PlotEmbedder) -> list[str]:
    """One heatmap per SensitivityResult (implied price across the grid)."""
    frags: list[str] = []
    for sens in (report.sensitivities or []):
        if sens is None:
            continue
        grid = getattr(sens, "grid", None)
        rows = getattr(sens, "row_values", None)
        cols = getattr(sens, "col_values", None)
        if not grid or not rows or not cols:
            continue
        # Row/col axis tick labels: terminal-growth / WACC / margin levels are
        # decimals -> percentages; anything else (e.g. exit multiples) -> number.
        row_label = _esc(getattr(sens, "row_label", "") or "")
        col_label = _esc(getattr(sens, "col_label", "") or "")
        row_text = [_axis_tick(v, getattr(sens, "row_label", "")) for v in rows]
        col_text = [_axis_tick(v, getattr(sens, "col_label", "")) for v in cols]

        # Cell text: formatted implied prices (blank when nan/None).
        cell_text = [
            [_fmt_price(v, symbol) if is_num(v) else "" for v in (grow or [])]
            for grow in grid
        ]
        fig = go.Figure(
            go.Heatmap(
                z=grid,
                x=col_text,
                y=row_text,
                text=cell_text,
                texttemplate="%{text}",
                colorscale="RdYlGn",
                colorbar=dict(title="Implied " + (symbol or "price")),
                hovertemplate=(
                    col_label + ": %{x}<br>" + row_label
                    + ": %{y}<br>Implied: " + symbol + "%{z:,.2f}<extra></extra>"
                ),
            )
        )
        fig.update_layout(
            title=_esc(getattr(sens, "title", "") or "Sensitivity"),
            xaxis_title=col_label,
            yaxis_title=row_label,
            height=360,
            margin=dict(l=10, r=20, t=60, b=50),
            paper_bgcolor="white",
            font=dict(family="Segoe UI, Helvetica, Arial, sans-serif", size=13),
        )
        # Keep rows top-to-bottom in the order provided.
        fig.update_yaxes(autorange="reversed")
        frags.append(embed.to_html(fig))
    return frags


def _axis_tick(value: Optional[float], label: str) -> str:
    """Format a sensitivity axis level. Rate-like axes -> %, else a number/multiple."""
    if not is_num(value):
        return "n/a"
    low = (label or "").lower()
    if any(k in low for k in ("wacc", "growth", "margin", "rate", "discount")):
        return _fmt_pct(value, 2)
    if "multiple" in low or "ebitda" in low or "ev/" in low:
        return _fmt_mult(value)
    # Small decimals are almost always rates; render as percent to be safe.
    if abs(value) < 1.0:
        return _fmt_pct(value, 2)
    return _fmt_num(value)


# --------------------------------------------------------------------------- #
#  Table builders (return HTML <table> fragments, '' when not applicable)
# --------------------------------------------------------------------------- #
def _dcf_table(report: ValuationReport, symbol: str) -> str:
    """Year-by-year DCF projection table + a value-bridge summary."""
    dcf = report.dcf
    if dcf is None:
        return ""
    years = list(getattr(dcf, "years", []) or [])
    if not years:
        return ""

    def col(attr: str) -> list:
        seq = list(getattr(dcf, attr, []) or [])
        # Pad/truncate to the number of years for safe zipping.
        return [seq[i] if i < len(seq) else None for i in range(len(years))]

    revenue = col("revenue")
    ebit = col("ebit")
    nopat = col("nopat")
    fcff = col("fcff")
    dfs = col("discount_factors")
    pv = col("pv_fcff")
    # EBIT margin per year for context.
    margins = [
        (e / r) if (is_num(e) and is_num(r) and r != 0) else None
        for e, r in zip(ebit, revenue)
    ]

    # Header row.
    header = "".join(f"<th>{_esc(y)}</th>" for y in years)
    rows_html = []

    def add_row(label: str, vals: list, fmt) -> None:
        cells = "".join(f"<td>{fmt(v)}</td>" for v in vals)
        rows_html.append(f"<tr><th class='rowhead'>{_esc(label)}</th>{cells}</tr>")

    add_row("Revenue", revenue, lambda v: _fmt_big(v, symbol))
    add_row("EBIT", ebit, lambda v: _fmt_big(v, symbol))
    add_row("EBIT margin", margins, lambda v: _fmt_pct(v))
    add_row("NOPAT", nopat, lambda v: _fmt_big(v, symbol))
    add_row("FCFF", fcff, lambda v: _fmt_big(v, symbol))
    add_row("Discount factor", dfs, lambda v: _fmt_num(v, 3))
    add_row("PV of FCFF", pv, lambda v: _fmt_big(v, symbol))

    table = (
        "<table class='grid'><thead><tr><th class='rowhead'>Year</th>"
        f"{header}</tr></thead><tbody>{''.join(rows_html)}</tbody></table>"
    )

    # Value bridge (EV -> equity -> per share). The model subtracts every senior
    # claim, so show minority interest and preferred equity next to net debt.
    bs = getattr(getattr(report, "company", None), "balance_sheet", None)

    def _claim(attr: str) -> float:
        v = getattr(bs, attr, None) if bs is not None else None
        return float(v) if is_num(v) else 0.0

    bridge_items = [
        ("Sum PV of FCFF", _fmt_big(sum(v for v in pv if is_num(v)), symbol)),
        ("Terminal value (undiscounted)", _fmt_big(getattr(dcf, "terminal_value", None), symbol)),
        ("PV of terminal value", _fmt_big(getattr(dcf, "pv_terminal", None), symbol)),
        ("Enterprise value", _fmt_big(getattr(dcf, "enterprise_value", None), symbol)),
        ("Less: net debt", _fmt_big(getattr(dcf, "net_debt", None), symbol)),
        ("Less: minority interest", _fmt_big(_claim("minority_interest"), symbol)),
        ("Less: preferred equity", _fmt_big(_claim("preferred_equity"), symbol)),
        ("Equity value", _fmt_big(getattr(dcf, "equity_value", None), symbol)),
        ("Shares", _fmt_big(getattr(dcf, "shares", None))),
        ("Implied price", _fmt_price(getattr(dcf, "implied_price", None), symbol)),
        ("Current price", _fmt_price(getattr(dcf, "current_price", None), symbol)),
    ]
    bridge_rows = "".join(
        f"<tr><th class='rowhead'>{_esc(k)}</th><td>{v}</td></tr>"
        for k, v in bridge_items
    )
    up = getattr(dcf, "upside", None)
    bridge_rows += (
        f"<tr><th class='rowhead'>Upside</th>"
        f"<td class='{_upside_class(up)}'>{_fmt_pct(up)}</td></tr>"
    )
    bridge = (
        "<table class='kv'><tbody>" + bridge_rows + "</tbody></table>"
    )

    return (
        "<div class='table-wrap'>" + table + "</div>"
        "<div class='bridge'>" + bridge + "</div>"
    )


def _comps_table(report: ValuationReport, symbol: str) -> str:
    """Target + peers multiples, the stats block, and implied prices."""
    comps = report.comps
    if comps is None:
        return ""

    mult_keys = list(config.COMPS_MULTIPLES)
    mult_labels = {
        "ev_ebitda": "EV/EBITDA",
        "ev_sales": "EV/Sales",
        "pe": "P/E",
        "pb": "P/B",
        "peg": "PEG",
    }
    headers = ["Company", "Market cap", "EV"] + [mult_labels.get(k, k) for k in mult_keys]
    head_html = "".join(f"<th>{_esc(h)}</th>" for h in headers)

    def row_cells(row, highlight: bool = False) -> str:
        if row is None:
            return ""
        name = _esc(getattr(row, "ticker", None) or getattr(row, "name", "?"))
        cells = [
            f"<td class='rowhead'>{name}</td>",
            f"<td>{_fmt_big(getattr(row, 'market_cap', None), symbol)}</td>",
            f"<td>{_fmt_big(getattr(row, 'enterprise_value', None), symbol)}</td>",
        ]
        for k in mult_keys:
            # PEG is a dimensionless ratio, not a turns multiple -> plain number.
            fmt = _fmt_num if k == "peg" else _fmt_mult
            cells.append(f"<td>{fmt(getattr(row, k, None))}</td>")
        cls = " class='target-row'" if highlight else ""
        return f"<tr{cls}>{''.join(cells)}</tr>"

    peers = getattr(comps, "peers", []) or []
    body = [row_cells(getattr(comps, "target", None), highlight=True)]
    for peer in peers:
        body.append(row_cells(peer))

    # Stats footer (median/mean/min/max/p25/p75) per multiple.
    stats = getattr(comps, "stats", {}) or {}
    stat_keys = ("median", "mean", "min", "max", "p25", "p75")
    stat_labels = {"median": "Median", "mean": "Mean", "min": "Min",
                   "max": "Max", "p25": "25th pct", "p75": "75th pct"}
    stat_rows = []
    # Without peers there are no statistics; skip the footer instead of
    # rendering rows of n/a under the target.
    for sk in (stat_keys if peers else ()):
        cells = [f"<td class='rowhead'>{stat_labels[sk]}</td><td></td><td></td>"]
        for mk in mult_keys:
            sub = stats.get(mk) or {}
            # PEG is a dimensionless ratio, not a turns multiple -> plain number.
            fmt = _fmt_num if mk == "peg" else _fmt_mult
            cells.append(f"<td>{fmt(sub.get(sk))}</td>")
        stat_rows.append(f"<tr class='stat-row'>{''.join(cells)}</tr>")

    table = (
        "<table class='grid'><thead><tr>" + head_html + "</tr></thead><tbody>"
        + "".join(body) + "".join(stat_rows) + "</tbody></table>"
    )

    # Implied prices per multiple + the low/median/high summary.
    implied = getattr(comps, "implied", {}) or {}
    imp_rows = []
    for k in mult_keys:
        price = implied.get(k)
        if is_num(price):
            imp_rows.append(
                f"<tr><th class='rowhead'>{_esc(mult_labels.get(k, k))}</th>"
                f"<td>{_fmt_price(price, symbol)}</td></tr>"
            )
    summary = getattr(comps, "implied_price_summary", {}) or {}
    for sk, lbl in (("low", "Low"), ("median", "Median"), ("high", "High")):
        if is_num(summary.get(sk)):
            imp_rows.append(
                f"<tr class='stat-row'><th class='rowhead'>{lbl} implied</th>"
                f"<td>{_fmt_price(summary.get(sk), symbol)}</td></tr>"
            )
    implied_block = ""
    if imp_rows:
        implied_block = (
            "<h3 class='subhead'>Implied price by multiple (peer median)</h3>"
            "<table class='kv'><tbody>" + "".join(imp_rows) + "</tbody></table>"
        )

    notes = getattr(comps, "notes", []) or []
    notes_block = ""
    if notes:
        items = "".join(f"<li>{_esc(n)}</li>" for n in notes)
        notes_block = f"<ul class='notes'>{items}</ul>"

    return (
        "<div class='table-wrap'>" + table + "</div>" + implied_block + notes_block
    )


# --------------------------------------------------------------------------- #
#  Header / summary + footnotes
# --------------------------------------------------------------------------- #
def _blended_target(report: ValuationReport) -> tuple[Optional[float], Optional[float]]:
    """(blended target, upside). Prefer report.summary; else median of FF bases."""
    summary = report.summary or {}
    # Source the header's blended target from report.summary so it matches the
    # Excel Summary sheet and the CLI. A None there means no method produced a
    # usable price: show n/a rather than inventing a target.
    target = None
    if "blended_target" in summary:
        if is_num(summary.get("blended_target")):
            target = float(summary["blended_target"])
    else:
        # No engine summary at all: fall back to the median of the valuation
        # methods' football-field bases. The 52-week row is market data (its
        # base is the current price), not a valuation, so it is excluded.
        bases = [getattr(r, "base", None) for r in (report.football_field or [])
                 if "52-w" not in str(getattr(r, "method", "") or "").lower()]
        target = median([b for b in bases if is_num(b)])

    # Likewise take the summary's blended upside so the header agrees with the
    # Excel Summary sheet.
    upside = None
    if target is not None and is_num(summary.get("blended_upside")):
        upside = float(summary["blended_upside"])
    if upside is None and is_num(target) and is_num(report.current_price) and report.current_price:
        upside = target / report.current_price - 1.0
    return target, upside


def _header_html(report: ValuationReport, symbol: str) -> str:
    company = report.company
    name = _esc(getattr(company, "name", None) or "")
    ticker = _esc(getattr(company, "ticker", None) or "")
    market = getattr(company, "market", None)
    currency = getattr(market, "currency", None) if market is not None else None
    target, upside = _blended_target(report)

    cards = [
        ("Current price", _fmt_price(report.current_price, symbol), "neutral"),
        ("Blended target", _fmt_price(target, symbol), "neutral"),
        ("Upside / downside", _fmt_pct(upside), _upside_class(upside)),
    ]
    cards_html = "".join(
        f"<div class='card'><div class='card-label'>{_esc(lbl)}</div>"
        f"<div class='card-value {cls}'>{val}</div></div>"
        for lbl, val, cls in cards
    )
    sub = " · ".join(filter(None, [ticker, _esc(currency or "")]))
    return (
        "<header class='report-header'>"
        f"<h1>{name}</h1>"
        f"<div class='subtitle'>{sub}</div>"
        f"<div class='cards'>{cards_html}</div>"
        + _methods_table(report, symbol)
        + "</header>"
    )


def _methods_table(report: ValuationReport, symbol: str) -> str:
    """Each method's implied price and upside: the inputs to the blended target."""
    methods = (report.summary or {}).get("methods") or {}
    if not methods:
        return ""
    cur = report.current_price
    rows = []
    for name, price in methods.items():
        up = (price / cur - 1.0) if (is_num(price) and is_num(cur) and cur) else None
        rows.append(
            f"<tr><th class='rowhead'>{_esc(name)}</th>"
            f"<td>{_fmt_price(price, symbol)}</td>"
            f"<td class='{_upside_class(up)}'>{_fmt_pct(up)}</td></tr>"
        )
    return (
        "<h3 class='subhead'>Valuation by method</h3>"
        "<table class='kv methods'><thead><tr><th></th><th>Implied price</th>"
        "<th>Upside</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _footnotes_html(report: ValuationReport) -> str:
    """Assumptions block (macro + DCF) and the list of report warnings."""
    items = []

    macro = report.macro
    if macro is not None:
        items.append(("Risk-free rate", _fmt_pct(getattr(macro, "risk_free_rate", None), 2)))
        items.append(("Equity risk premium", _fmt_pct(getattr(macro, "equity_risk_premium", None), 2)))
        if is_num(getattr(macro, "tax_rate", None)):
            items.append(("Tax rate", _fmt_pct(macro.tax_rate, 1)))
        if is_num(getattr(macro, "pretax_cost_of_debt", None)):
            items.append(("Pre-tax cost of debt", _fmt_pct(macro.pretax_cost_of_debt, 2)))

    # Pull a few key DCF assumptions if the model ran.
    dcf = report.dcf
    if dcf is not None:
        wacc = getattr(dcf, "wacc", None)
        if wacc is not None and is_num(getattr(wacc, "wacc", None)):
            items.append(("WACC", _fmt_pct(wacc.wacc, 2)))
            if is_num(getattr(wacc, "cost_of_equity", None)):
                items.append(("Cost of equity", _fmt_pct(wacc.cost_of_equity, 2)))
        adict = getattr(dcf, "assumptions", {}) or {}
        # The growth actually used (the model clamps it below WACC when needed).
        g_used = adict.get("terminal_growth_used", adict.get("terminal_growth"))
        if is_num(g_used):
            items.append(("Terminal growth", _fmt_pct(g_used, 2)))
        if adict.get("terminal_method"):
            items.append(("Terminal method", _esc(adict.get("terminal_method"))))
        if is_num(adict.get("tax_rate")):
            items.append(("DCF tax rate", _fmt_pct(adict.get("tax_rate"), 1)))

    assumptions_block = ""
    if items:
        rows = "".join(
            f"<tr><th class='rowhead'>{_esc(k)}</th><td>{v}</td></tr>"
            for k, v in items
        )
        assumptions_block = (
            "<h3 class='subhead'>Key assumptions</h3>"
            "<table class='kv'><tbody>" + rows + "</tbody></table>"
        )

    # Source notes from the data provider, if any.
    src_notes = getattr(getattr(report, "company", None), "source_notes", None) or []
    warnings = report.warnings or []
    note_items = []
    # The engine already merges source notes into report.warnings; only render
    # the ones that did not make it there so nothing appears twice.
    for n in src_notes:
        if n not in warnings:
            note_items.append(f"<li>{_esc(n)}</li>")
    for w in warnings:
        note_items.append(f"<li class='warn'>{_esc(w)}</li>")
    notes_block = ""
    if note_items:
        notes_block = (
            "<h3 class='subhead'>Notes &amp; warnings</h3>"
            "<ul class='notes'>" + "".join(note_items) + "</ul>"
        )

    disclaimer = (
        "<p class='disclaimer'>This report is generated for educational and "
        "research purposes only and does not constitute investment advice. "
        "Figures are model estimates based on the assumptions above and the "
        "data sources noted; they may be incomplete or inaccurate.</p>"
    )
    return assumptions_block + notes_block + disclaimer


# --------------------------------------------------------------------------- #
#  Page assembly
# --------------------------------------------------------------------------- #
_STYLE = """
:root {
  --ink:#1c2733; --muted:#5b6876; --line:#e3e8ee; --accent:#16324f;
  --bg:#f5f7fa; --pos:#1a7f37; --neg:#c0392b;
}
* { box-sizing: border-box; }
body {
  margin:0; background:var(--bg); color:var(--ink);
  font-family:"Segoe UI", Helvetica, Arial, sans-serif; font-size:14px; line-height:1.45;
}
.container { max-width:1080px; margin:0 auto; padding:28px 24px 64px; }
.report-header { border-bottom:3px solid var(--accent); padding-bottom:18px; margin-bottom:8px; }
.report-header h1 { margin:0 0 2px; font-size:26px; letter-spacing:-0.01em; }
.subtitle { color:var(--muted); font-size:14px; font-weight:600; letter-spacing:0.04em; text-transform:uppercase; }
.cards { display:flex; gap:14px; margin-top:16px; flex-wrap:wrap; }
.card {
  background:white; border:1px solid var(--line); border-radius:8px;
  padding:12px 18px; min-width:160px; flex:1;
}
.card-label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:0.05em; }
.card-value { font-size:22px; font-weight:700; margin-top:4px; }
.section { background:white; border:1px solid var(--line); border-radius:10px;
  padding:18px 20px; margin-top:22px; }
.section > h2 { margin:0 0 12px; font-size:18px; color:var(--accent);
  border-bottom:1px solid var(--line); padding-bottom:8px; }
.subhead { font-size:14px; margin:18px 0 8px; color:var(--accent); }
.table-wrap { overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-size:13px; }
table.grid th, table.grid td { border:1px solid var(--line); padding:6px 9px; text-align:right; }
table.grid thead th { background:var(--accent); color:white; text-align:right; }
table.grid th.rowhead { text-align:left; background:#eef2f7; color:var(--ink); font-weight:600; }
table.grid tr.target-row td { background:#fff5f3; font-weight:600; }
table.grid tr.stat-row td { background:#f3f7fb; font-style:italic; color:var(--muted); }
table.kv { width:auto; min-width:280px; }
table.kv th.rowhead { text-align:left; padding:5px 14px 5px 0; color:var(--muted); font-weight:600; }
table.kv td { text-align:right; padding:5px 0; font-weight:600; }
table.kv.methods td { padding-left:18px; }
table.kv.methods thead th { text-align:right; color:var(--muted); font-size:12px; font-weight:600; padding-left:18px; }
.bridge { margin-top:14px; }
.pos { color:var(--pos); }
.neg { color:var(--neg); }
.neutral { color:var(--ink); }
ul.notes { margin:8px 0; padding-left:20px; color:var(--muted); font-size:13px; }
ul.notes li.warn { color:var(--neg); }
.disclaimer { margin-top:20px; font-size:11px; color:var(--muted); border-top:1px solid var(--line); padding-top:12px; }
.empty { color:var(--muted); font-style:italic; }
footer.gen { margin-top:30px; color:var(--muted); font-size:11px; text-align:center; }
"""

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<style>{style}</style>
</head>
<body>
<div class="container">
{header}
{football}
{dcf_section}
{comps_section}
{sens_section}
{footnotes}
<footer class="gen">Generated {generated} · equity-valuation</footer>
</div>
</body>
</html>
"""


def _section(title: str, *fragments: str) -> str:
    """Wrap non-empty fragments in a titled section card; '' if all empty."""
    body = "".join(f for f in fragments if f)
    if not body.strip():
        return ""
    return f"<section class='section'><h2>{_esc(title)}</h2>{body}</section>"


def write_html(report: ValuationReport, path: str) -> str:
    """Write a single self-contained interactive .html report and return the path.

    The document embeds plotly.js once so it renders offline. Every model section
    is optional: when ``report.dcf`` / ``report.comps`` / sensitivities are absent
    or empty, the corresponding section is simply omitted. The function never
    raises on missing fields — it degrades to ``n/a`` cells and skipped sections.
    """
    embed = _PlotEmbedder()  # per-render embed state (plotly.js once per document)

    # Resolve the currency symbol from the company's market data.
    market = getattr(getattr(report, "company", None), "market", None)
    currency = getattr(market, "currency", None) if market is not None else None
    symbol = _currency_symbol(currency)

    # --- Header ----------------------------------------------------------- #
    header = _header_html(report, symbol)

    # --- Football field (always its own section when data exists) --------- #
    ff_fig = _football_field_fig(report, symbol, embed)
    football = _section("Valuation summary", ff_fig) if ff_fig else ""

    # --- DCF -------------------------------------------------------------- #
    dcf_table = _dcf_table(report, symbol)
    dcf_chart = _fcff_fig(report, symbol, embed)
    dcf_section = _section("Discounted cash flow (DCF)", dcf_table, dcf_chart)

    # --- Comps ------------------------------------------------------------ #
    comps_table = _comps_table(report, symbol)
    comps_chart = _comps_fig(report, embed)
    comps_section = _section("Trading comparables", comps_table, comps_chart)

    # --- Sensitivities ---------------------------------------------------- #
    sens_figs = _sensitivity_figs(report, symbol, embed)
    sens_section = _section("Sensitivity analysis", *sens_figs) if sens_figs else ""

    # --- Footnotes / assumptions / warnings ------------------------------- #
    footnotes = _section("Assumptions & notes", _footnotes_html(report))

    company_name = getattr(getattr(report, "company", None), "name", None) or "Valuation report"
    ticker = getattr(getattr(report, "company", None), "ticker", None) or ""
    title = f"{company_name} ({ticker}) — Valuation" if ticker else f"{company_name} — Valuation"

    page = _PAGE_TEMPLATE.format(
        title=_esc(title),
        style=_STYLE,
        header=header,
        football=football,
        dcf_section=dcf_section,
        comps_section=comps_section,
        sens_section=sens_section,
        footnotes=footnotes,
        generated=_esc(datetime.date.today().isoformat()),
    )

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return path
