"use client";

// Historical financials panel: income statement & cash-flow series plus the
// balance-sheet snapshot. Mirrors ValuationSummary's structure and density.

import React from "react";
import type { Report } from "@/lib/types";
import { fmtBig, fmtDate, fmtPct } from "@/lib/format";
import { Card, EmptyState, Stat, Table, TD, TH } from "@/components/ui";

type RowKind = "money" | "shares" | "pct";

interface RowDef {
  label: string;
  // Returns the raw numeric value for a given year index, or null when absent.
  value: (i: number) => number | null;
  kind: RowKind;
  emphasis?: boolean; // derived sub-metrics (margins/growth) get a dimmer label
}

function at(arr: number[] | null | undefined, i: number): number | null {
  const v = arr?.[i];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function ratio(
  num: number | null,
  den: number | null
): number | null {
  if (num === null || den === null || den === 0) return null;
  return num / den;
}

export default function FinancialsPanel({ report }: { report: Report }) {
  const cur = report.summary.currency;
  const f = report.company.financials;
  const years = f?.fiscal_years ?? [];
  const bs = report.company.balance_sheet;
  const notes = report.company.source_notes ?? [];

  if (years.length === 0) {
    return (
      <Card title="Income statement & cash flow" subtitle="FY, absolute units">
        <EmptyState
          title="No historical financials"
          hint="The valuation engine returned no fiscal-year series for this company."
        />
      </Card>
    );
  }

  const rows: RowDef[] = [
    { label: "Revenue", kind: "money", value: (i) => at(f?.revenue, i) },
    {
      label: "Revenue growth",
      kind: "pct",
      emphasis: true,
      value: (i) => {
        if (i <= 0) return null;
        const r = ratio(at(f?.revenue, i), at(f?.revenue, i - 1));
        return r === null ? null : r - 1;
      },
    },
    { label: "EBIT", kind: "money", value: (i) => at(f?.ebit, i) },
    {
      label: "EBIT margin",
      kind: "pct",
      emphasis: true,
      value: (i) => ratio(at(f?.ebit, i), at(f?.revenue, i)),
    },
    { label: "EBITDA", kind: "money", value: (i) => at(f?.ebitda, i) },
    { label: "Net income", kind: "money", value: (i) => at(f?.net_income, i) },
    {
      label: "Net margin",
      kind: "pct",
      emphasis: true,
      value: (i) => ratio(at(f?.net_income, i), at(f?.revenue, i)),
    },
    { label: "D&A", kind: "money", value: (i) => at(f?.dep_amort, i) },
    { label: "Capex", kind: "money", value: (i) => at(f?.capex, i) },
    { label: "Δ NWC", kind: "money", value: (i) => at(f?.change_in_nwc, i) },
    {
      label: "Interest expense",
      kind: "money",
      value: (i) => at(f?.interest_expense, i),
    },
    { label: "Tax expense", kind: "money", value: (i) => at(f?.tax_expense, i) },
    {
      label: "Pretax income",
      kind: "money",
      value: (i) => at(f?.pretax_income, i),
    },
    {
      label: "Dividends paid",
      kind: "money",
      value: (i) => at(f?.dividends_paid, i),
    },
    {
      label: "Diluted shares",
      kind: "shares",
      value: (i) => at(f?.diluted_shares, i),
    },
  ];

  function renderCell(row: RowDef, i: number): string {
    const v = row.value(i);
    switch (row.kind) {
      case "money":
        return fmtBig(v, cur);
      case "shares":
        return fmtBig(v);
      case "pct":
        return fmtPct(v, { signed: row.label === "Revenue growth" });
    }
  }

  return (
    <div className="grid grid-cols-1 gap-4">
      <Card
        title="Income statement & cash flow"
        subtitle="FY, absolute units"
      >
        <Table>
          <thead>
            <tr>
              <TH align="left">FY</TH>
              {years.map((y) => (
                <TH key={y}>{y}</TH>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.label}>
                <TH align="left" className="font-medium normal-case">
                  <span className={row.emphasis ? "text-ink-faint" : "text-ink-dim"}>
                    {row.label}
                  </span>
                </TH>
                {years.map((y, i) => (
                  <TD
                    key={y}
                    num
                    className={row.emphasis ? "text-ink-dim" : undefined}
                  >
                    {renderCell(row, i)}
                  </TD>
                ))}
              </tr>
            ))}
          </tbody>
        </Table>

        {notes.length > 0 && (
          <div className="mt-3 space-y-0.5 border-t border-line pt-2">
            {notes.map((n, i) => (
              <p key={i} className="text-xs text-ink-faint">
                {n}
              </p>
            ))}
          </div>
        )}
      </Card>

      <Card
        title="Balance sheet snapshot"
        subtitle={bs?.as_of ? `as of ${fmtDate(bs.as_of)}` : "as of —"}
      >
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
          <Stat label="Total debt" value={fmtBig(bs?.total_debt, cur)} />
          <Stat
            label="Cash & investments"
            value={fmtBig(bs?.cash_and_investments, cur)}
          />
          <Stat label="Net debt" value={fmtBig(bs?.net_debt, cur)} />
          <Stat label="Total equity" value={fmtBig(bs?.total_equity, cur)} />
          <Stat
            label="Minority interest"
            value={fmtBig(bs?.minority_interest, cur)}
          />
          <Stat label="Preferred" value={fmtBig(bs?.preferred_equity, cur)} />
        </div>
      </Card>
    </div>
  );
}
