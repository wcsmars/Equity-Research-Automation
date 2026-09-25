// Formatting helpers shared by every panel. All tolerate null/undefined/NaN.

const SYMBOLS: Record<string, string> = {
  USD: "$",
  EUR: "€",
  GBP: "£",
  JPY: "¥",
  CAD: "C$",
  AUD: "A$",
  CHF: "CHF ",
  CNY: "¥",
  HKD: "HK$",
  INR: "₹",
};

// Unmapped currencies fall back to their ISO code ("SEK 33.74") so a figure
// never renders without any currency marker.
export function sym(currency?: string | null): string {
  const c = currency || "USD";
  return SYMBOLS[c] ?? `${c} `;
}

function isNum(x: unknown): x is number {
  return typeof x === "number" && Number.isFinite(x);
}

// Snap values that round to zero at the displayed precision to +0, so tiny
// negatives render as "$0.00" / "0.0%" rather than "-$0.00" / "-0.0%".
function snap(x: number, decimals: number): number {
  return Number(x.toFixed(decimals)) === 0 ? 0 : x;
}

// The minus sign goes before the currency symbol: -$36.50, not $-36.50.
export function fmtMoney(
  x: number | null | undefined,
  currency?: string | null,
  decimals = 2
): string {
  if (!isNum(x)) return "—";
  x = snap(x, decimals);
  const sign = x < 0 ? "-" : "";
  return `${sign}${sym(currency)}${Math.abs(x).toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`;
}

// Compact magnitude without sign or symbol: 391.0B, 1.2T, 540.0M. The unit is
// picked on the rounded value, so 999.96B renders as 1.0T, not 1000.0B.
function compact(abs: number): string {
  const units: [string, number][] = [
    ["T", 1e12],
    ["B", 1e9],
    ["M", 1e6],
    ["K", 1e3],
  ];
  for (const [u, d] of units) {
    const v = (abs / d).toFixed(1);
    if (Number(v) >= 1) return `${v}${u}`;
  }
  return abs.toFixed(0);
}

// Compact money amounts: $391.0B, -$36.5B, €1.2T.
export function fmtBig(
  x: number | null | undefined,
  currency?: string | null
): string {
  if (!isNum(x)) return "—";
  x = snap(x, 0);
  return `${x < 0 ? "-" : ""}${sym(currency)}${compact(Math.abs(x))}`;
}

// Compact counts with no currency symbol (share counts): 7.4B, 540.0M.
export function fmtCount(x: number | null | undefined): string {
  if (!isNum(x)) return "—";
  x = snap(x, 0);
  return `${x < 0 ? "-" : ""}${compact(Math.abs(x))}`;
}

export function fmtPct(
  x: number | null | undefined,
  opts: { signed?: boolean; decimals?: number } = {}
): string {
  if (!isNum(x)) return "—";
  const { signed = false, decimals = 1 } = opts;
  const p = snap(x * 100, decimals);
  const sign = signed && p > 0 ? "+" : "";
  return `${sign}${p.toFixed(decimals)}%`;
}

export function fmtNum(
  x: number | null | undefined,
  decimals = 2
): string {
  if (!isNum(x)) return "—";
  return snap(x, decimals).toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

// Trading multiple, e.g. 12.3x.
export function fmtMult(x: number | null | undefined, decimals = 1): string {
  if (!isNum(x)) return "—";
  return `${snap(x, decimals).toFixed(decimals)}x`;
}

export function fmtDate(s: string | null | undefined): string {
  if (!s) return "";
  // A date-only ISO string ("2024-12-31") parses as UTC midnight, which
  // renders as the previous day west of UTC; build it as a local date instead.
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  const d = m ? new Date(+m[1], +m[2] - 1, +m[3]) : new Date(s);
  if (Number.isNaN(d.getTime())) return s;
  if (m && (d.getMonth() !== +m[2] - 1 || d.getDate() !== +m[3])) return s;
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

// Map an upside/recommendation to a semantic color class.
export function toneForUpside(upside: number | null | undefined): string {
  if (!isNum(upside)) return "text-ink-dim";
  if (upside >= 0.15) return "text-up";
  if (upside <= -0.15) return "text-down";
  return "text-flat";
}

export function toneForRecommendation(rec: string | null | undefined): string {
  switch (rec) {
    case "Undervalued":
      return "text-up";
    case "Overvalued":
      return "text-down";
    case "Fairly valued":
      return "text-flat";
    default:
      return "text-ink-dim";
  }
}
