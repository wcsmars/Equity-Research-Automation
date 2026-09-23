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

export function sym(currency?: string | null): string {
  return SYMBOLS[currency || "USD"] ?? "";
}

function isNum(x: unknown): x is number {
  return typeof x === "number" && Number.isFinite(x);
}

// The minus sign goes before the currency symbol: -$36.50, not $-36.50.
export function fmtMoney(
  x: number | null | undefined,
  currency?: string | null,
  decimals = 2
): string {
  if (!isNum(x)) return "—";
  const sign = x < 0 ? "-" : "";
  return `${sign}${sym(currency)}${Math.abs(x).toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`;
}

// Compact magnitude without sign or symbol: 391.0B, 1.2T, 540.0M.
function compact(abs: number): string {
  const units: [string, number][] = [
    ["T", 1e12],
    ["B", 1e9],
    ["M", 1e6],
    ["K", 1e3],
  ];
  for (const [u, d] of units) {
    if (abs >= d) return `${(abs / d).toFixed(1)}${u}`;
  }
  return abs.toFixed(0);
}

// Compact money amounts: $391.0B, -$36.5B, €1.2T.
export function fmtBig(
  x: number | null | undefined,
  currency?: string | null
): string {
  if (!isNum(x)) return "—";
  return `${x < 0 ? "-" : ""}${sym(currency)}${compact(Math.abs(x))}`;
}

// Compact counts with no currency symbol (share counts): 7.4B, 540.0M.
export function fmtCount(x: number | null | undefined): string {
  if (!isNum(x)) return "—";
  return `${x < 0 ? "-" : ""}${compact(Math.abs(x))}`;
}

export function fmtPct(
  x: number | null | undefined,
  opts: { signed?: boolean; decimals?: number } = {}
): string {
  if (!isNum(x)) return "—";
  const { signed = false, decimals = 1 } = opts;
  const v = (x * 100).toFixed(decimals);
  const sign = signed && x > 0 ? "+" : "";
  return `${sign}${v}%`;
}

export function fmtNum(
  x: number | null | undefined,
  decimals = 2
): string {
  if (!isNum(x)) return "—";
  return x.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

// Trading multiple, e.g. 12.3x.
export function fmtMult(x: number | null | undefined, decimals = 1): string {
  if (!isNum(x)) return "—";
  return `${x.toFixed(decimals)}x`;
}

export function fmtDate(s: string | null | undefined): string {
  if (!s) return "";
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return s;
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
