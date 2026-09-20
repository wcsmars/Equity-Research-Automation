"use client";

// Sensitivity heatmaps. One Card per grid in report.sensitivities, each
// rendered as a color-coded HTML table (price implied at row/col axis pairs).
// Green = undervalued (price above current), rose = overvalued. Follows the
// structure, density, and color usage of ValuationSummary.

import React from "react";
import type { Report, Sensitivity } from "@/lib/types";
import { fmtMoney, fmtPct } from "@/lib/format";
import { cx, Card, EmptyState } from "@/components/ui";

// Background color for a cell, keyed off upside vs. current price.
function cellStyle(
  price: number | null | undefined,
  currentPrice: number | null | undefined
): React.CSSProperties {
  if (
    typeof price !== "number" ||
    !Number.isFinite(price) ||
    typeof currentPrice !== "number" ||
    !Number.isFinite(currentPrice) ||
    currentPrice === 0
  ) {
    return {};
  }
  const u = price / currentPrice - 1;
  const t = Math.max(-0.5, Math.min(0.5, u));
  const alpha = Math.min(0.35, Math.abs(t) * 0.7);
  const backgroundColor =
    t >= 0
      ? `rgba(52, 211, 153, ${alpha})` // green / up
      : `rgba(251, 113, 133, ${alpha})`; // rose / down
  return { backgroundColor };
}

function SensitivityGrid({
  s,
  currentPrice,
  cur,
}: {
  s: Sensitivity;
  currentPrice: number;
  cur: string;
}) {
  const rowValues = s.row_values || [];
  const colValues = s.col_values || [];
  const grid = s.grid || [];

  if (rowValues.length === 0 || colValues.length === 0 || grid.length === 0) {
    return (
      <EmptyState
        title="Grid unavailable"
        hint="This sensitivity grid has no data."
      />
    );
  }

  // The center cell is the base case — highlight it lightly.
  const centerRow = Math.floor(rowValues.length / 2);
  const centerCol = Math.floor(colValues.length / 2);

  return (
    <div className="overflow-x-auto">
      <table className="num w-full border-collapse text-xs">
        <thead>
          <tr>
            <th className="whitespace-nowrap border border-line/60 bg-surface-raised px-2.5 py-1.5 text-left text-[10px] font-medium uppercase tracking-wider text-ink-faint">
              {s.row_label} {"\\"} {s.col_label}
            </th>
            {colValues.map((c, j) => (
              <th
                key={j}
                className="whitespace-nowrap border border-line/60 bg-surface-raised px-2.5 py-1.5 text-center text-[11px] font-semibold text-ink-dim"
              >
                {fmtPct(c)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rowValues.map((r, i) => {
            const row = grid[i] || [];
            return (
              <tr key={i}>
                <th className="whitespace-nowrap border border-line/60 bg-surface-raised px-2.5 py-1.5 text-right text-[11px] font-semibold text-ink-dim">
                  {fmtPct(r)}
                </th>
                {colValues.map((_c, j) => {
                  const price = row[j] ?? null;
                  const isCenter = i === centerRow && j === centerCol;
                  return (
                    <td
                      key={j}
                      style={cellStyle(price, currentPrice)}
                      className={cx(
                        "whitespace-nowrap border border-line/60 px-2.5 py-1.5 text-center text-ink",
                        isCenter && "font-semibold ring-1 ring-inset ring-ink/40"
                      )}
                    >
                      {fmtMoney(price, cur, 0)}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default function SensitivityPanel({ report }: { report: Report }) {
  const grids = report.sensitivities || [];
  const cur = report.summary.currency;
  const currentPrice = report.current_price;

  if (grids.length === 0) {
    return (
      <Card title="Sensitivity">
        <EmptyState
          title="No sensitivity grids"
          hint="Sensitivity needs a viable DCF."
        />
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {grids.map((s, idx) => (
        <Card
          key={s.title || idx}
          title={s.title}
          subtitle={`Implied price by ${s.row_label} and ${s.col_label} · shaded vs. current ${fmtMoney(
            currentPrice,
            cur,
            0
          )}`}
        >
          <SensitivityGrid s={s} currentPrice={currentPrice} cur={cur} />
        </Card>
      ))}
    </div>
  );
}
