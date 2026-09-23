"use client";

// Dividend discount model (DDM) + levered FCFE panel. Follows the structure,
// density, and color usage of ValuationSummary (the reference panel).

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
import { Card, EmptyState, Stat, Table, TD, TH } from "@/components/ui";

// Humanize a snake_case detail key, e.g. "terminal_growth" -> "terminal growth".
function humanizeKey(key: string): string {
  return key.replace(/_/g, " ");
}

// Format an untrusted detail value. Numbers that look like rates/growth render
// as percentages; other numbers as plain figures; everything else as a string.
function fmtDetailValue(key: string, value: unknown): string {
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return "—";
    const k = key.toLowerCase();
    // Year counts (e.g. high_growth_years) are integers, not rates.
    if (k.includes("year")) return String(Math.round(value));
    const rateLike =
      /(growth|rate|margin|yield|return|wacc|cost|premium|payout|retention|equity_w|weight)/.test(
        k
      ) || k === "ke";
    if (rateLike && Math.abs(value) <= 1.5) return fmtPct(value);
    return fmtNum(value, 4);
  }
  if (value == null) return "—";
  return String(value);
}

function DetailList({ detail }: { detail: Record<string, unknown> | null | undefined }) {
  const entries = Object.entries(detail || {});
  if (entries.length === 0) return null;
  return (
    <div className="mt-3 grid grid-cols-1 gap-x-4 gap-y-1 border-t border-line pt-3 sm:grid-cols-2">
      {entries.map(([key, value]: [string, unknown]) => (
        <div key={key} className="flex items-baseline justify-between gap-3">
          <span className="text-xs capitalize text-ink-dim">
            {humanizeKey(key)}
          </span>
          <span className="num text-xs text-ink">
            {fmtDetailValue(key, value)}
          </span>
        </div>
      ))}
    </div>
  );
}

export default function DDMFCFEPanel({ report }: { report: Report }) {
  const cur = report.summary.currency;
  const price = report.current_price;
  const ddm = report.ddm;
  const fcfe = report.fcfe;

  const ddmUpside =
    ddm && price ? ddm.implied_price / price - 1 : null;
  const fcfeUpside =
    fcfe && price ? fcfe.implied_price / price - 1 : null;

  const fcfeYears = fcfe?.years || [];

  return (
    <div className="grid grid-cols-1 gap-4">
      <Card
        title="Dividend discount model (DDM)"
        subtitle="Implied value from projected dividends discounted at the cost of equity"
      >
        {ddm == null ? (
          <EmptyState
            title="DDM not applicable"
            hint="Company pays no (or negligible) dividend."
          />
        ) : (
          <>
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
              <Stat
                label="Implied price"
                value={fmtMoney(ddm.implied_price, cur)}
                tone={toneForUpside(ddmUpside)}
                sub={fmtPct(ddmUpside, { signed: true })}
              />
              <Stat
                label="Method"
                value={
                  <span className="text-sm">{ddm.method || "—"}</span>
                }
              />
              <Stat
                label="Cost of equity"
                value={fmtPct(ddm.cost_of_equity)}
              />
            </div>
            <DetailList detail={ddm.detail} />
          </>
        )}
      </Card>

      <Card
        title="Levered FCFE"
        subtitle="Free cash flow to equity discounted at the cost of equity"
      >
        {fcfe == null ? (
          <EmptyState
            title="FCFE unavailable"
            hint={(report.warnings || []).join(" · ")}
          />
        ) : (
          <>
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Stat
                label="Implied price"
                value={fmtMoney(fcfe.implied_price, cur)}
                tone={toneForUpside(fcfeUpside)}
                sub={fmtPct(fcfeUpside, { signed: true })}
              />
              <Stat
                label="Cost of equity"
                value={fmtPct(fcfe.cost_of_equity)}
              />
              <Stat
                label="Equity value"
                value={fmtBig(fcfe.equity_value, cur)}
              />
              <Stat label="Shares" value={fmtCount(fcfe.shares)} />
            </div>

            {fcfeYears.length > 0 && (
              <div className="mt-4">
                <Table>
                  <thead>
                    <tr>
                      <TH align="left">Year</TH>
                      {fcfeYears.map((y: number, i: number) => (
                        <TH key={i}>{y}</TH>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <TD align="left">FCFE</TD>
                      {fcfeYears.map((_y: number, i: number) => (
                        <TD key={i} num>
                          {fmtBig(fcfe.fcfe?.[i], cur)}
                        </TD>
                      ))}
                    </tr>
                    <tr>
                      <TD align="left">PV of FCFE</TD>
                      {fcfeYears.map((_y: number, i: number) => (
                        <TD key={i} num>
                          {fmtBig(fcfe.pv_fcfe?.[i], cur)}
                        </TD>
                      ))}
                    </tr>
                  </tbody>
                </Table>
              </div>
            )}

            <div className="mt-4 grid grid-cols-2 gap-x-4 gap-y-1 border-t border-line pt-3 sm:grid-cols-4">
              <div className="flex items-baseline justify-between gap-3">
                <span className="text-xs text-ink-dim">Terminal value</span>
                <span className="num text-xs text-ink">
                  {fmtBig(fcfe.terminal_value, cur)}
                </span>
              </div>
              <div className="flex items-baseline justify-between gap-3">
                <span className="text-xs text-ink-dim">PV terminal</span>
                <span className="num text-xs text-ink">
                  {fmtBig(fcfe.pv_terminal, cur)}
                </span>
              </div>
              <div className="flex items-baseline justify-between gap-3">
                <span className="text-xs text-ink-dim">Equity value</span>
                <span className="num text-xs text-ink">
                  {fmtBig(fcfe.equity_value, cur)}
                </span>
              </div>
              <div className="flex items-baseline justify-between gap-3">
                <span className="text-xs text-ink-dim">Implied price</span>
                <span className="num text-xs text-ink">
                  {fmtMoney(fcfe.implied_price, cur)}
                </span>
              </div>
            </div>

            <DetailList detail={fcfe.detail} />
          </>
        )}
      </Card>
    </div>
  );
}
