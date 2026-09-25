"use client";

// Overview panel + the football-field chart. This is the REFERENCE panel —
// other panels follow its structure, primitives, and color usage.

import React from "react";
import type { Report } from "@/lib/types";
import {
  fmtBig,
  fmtCount,
  fmtMoney,
  fmtNum,
  fmtPct,
  toneForUpside,
} from "@/lib/format";
import { Badge, Card, Stat, Table, TD, TH } from "@/components/ui";

function FootballField({ report }: { report: Report }) {
  const rows = report.football_field || [];
  const price = report.current_price;
  const cur = report.summary.currency;
  if (rows.length === 0) {
    return <p className="text-sm text-ink-faint">No valuation ranges available.</p>;
  }

  const lows = rows.map((r) => r.low);
  const highs = rows.map((r) => r.high);
  let min = Math.min(...lows, price);
  let max = Math.max(...highs, price);
  const pad = (max - min) * 0.06 || 1;
  min -= pad;
  max += pad;
  const span = max - min || 1;
  const pos = (v: number) => ((v - min) / span) * 100;

  return (
    <div>
      <div className="space-y-3">
        {rows.map((r) => {
          const left = pos(r.low);
          const width = Math.max(pos(r.high) - left, 0.8);
          const tone =
            r.base >= price * 1.05
              ? "bg-up/30 border-up"
              : r.base <= price * 0.95
              ? "bg-down/30 border-down"
              : "bg-flat/30 border-flat";
          return (
            <div key={r.method} className="flex items-center gap-3">
              <div className="w-32 shrink-0 truncate text-right text-xs text-ink-dim">
                {r.method}
              </div>
              <div className="relative h-7 flex-1">
                <div
                  className={`absolute top-1/2 -translate-y-1/2 rounded border ${tone}`}
                  style={{ left: `${left}%`, width: `${width}%`, height: 12 }}
                />
                {/* base marker */}
                <div
                  className="absolute top-1/2 h-4 w-[2px] -translate-x-1/2 -translate-y-1/2 bg-ink"
                  style={{ left: `${pos(r.base)}%` }}
                  title={`base ${fmtMoney(r.base, cur)}`}
                />
                <div
                  className="num absolute top-1/2 -translate-y-1/2 text-[10px] text-ink-faint"
                  style={{ left: `calc(${pos(r.high)}% + 6px)` }}
                >
                  {fmtMoney(r.high, cur, 0)}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* current-price reference line spanning the chart */}
      <div className="mt-2 flex items-center gap-3">
        <div className="w-32 shrink-0" />
        <div className="relative h-5 flex-1">
          <div
            className="absolute top-0 h-5 w-[2px] -translate-x-1/2 bg-brand"
            style={{ left: `${pos(price)}%` }}
          />
          <div
            className="num absolute top-0 -translate-x-1/2 whitespace-nowrap text-[10px] text-brand"
            style={{ left: `${pos(price)}%` }}
          >
            price {fmtMoney(price, cur, 0)}
          </div>
        </div>
      </div>
    </div>
  );
}

export default function ValuationSummary({ report }: { report: Report }) {
  const s = report.summary;
  const m = report.company.market;
  const cur = s.currency;
  const methods = Object.entries(s.methods || {});

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
      <Card
        title="Valuation football field"
        subtitle="Implied value range by method vs. current price"
        className="lg:col-span-2"
      >
        <FootballField report={report} />
      </Card>

      <Card title="Method summary">
        <Table>
          <thead>
            <tr>
              <TH align="left">Method</TH>
              <TH>Implied</TH>
              <TH>Upside</TH>
            </tr>
          </thead>
          <tbody>
            {methods.map(([name, price]) => {
              const up =
                price != null && s.current_price
                  ? price / s.current_price - 1
                  : null;
              return (
                <tr key={name}>
                  <TD align="left">{name}</TD>
                  <TD num>{fmtMoney(price, cur)}</TD>
                  <TD num className={toneForUpside(up)}>
                    {fmtPct(up, { signed: true })}
                  </TD>
                </tr>
              );
            })}
            <tr className="font-semibold">
              <TD align="left">Blended target</TD>
              <TD num>{fmtMoney(s.blended_target, cur)}</TD>
              <TD num className={toneForUpside(s.blended_upside)}>
                {fmtPct(s.blended_upside, { signed: true })}
              </TD>
            </tr>
          </tbody>
        </Table>
      </Card>

      <Card title="Snapshot" className="lg:col-span-3">
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
          <Stat label="Market cap" value={fmtBig(m.market_cap, cur)} />
          <Stat label="Beta" value={fmtNum(m.beta, 2)} />
          <Stat
            label="52-week range"
            value={
              <span className="text-sm">
                {fmtMoney(m.fifty_two_week_low, cur, 0)} –{" "}
                {fmtMoney(m.fifty_two_week_high, cur, 0)}
              </span>
            }
          />
          <Stat
            label="Shares out"
            value={fmtCount(m.shares_outstanding)}
          />
          <Stat
            label="Dividend / sh"
            value={fmtMoney(m.dividend_per_share, cur)}
          />
          <Stat
            label="Sector"
            value={<span className="text-sm">{m.sector || "—"}</span>}
            sub={m.industry || undefined}
          />
        </div>
        {report.warnings.length > 0 && (
          <div className="mt-4 flex flex-wrap gap-2 border-t border-line pt-3">
            {report.warnings.map((w, i) => (
              <Badge key={i} tone="neutral">
                {w}
              </Badge>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
