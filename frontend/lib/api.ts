// Thin client for the FastAPI backend. The browser hits same-origin /api/*,
// which Next rewrites to the FastAPI server (see next.config.mjs).

import type {
  Assumptions,
  ChatTurn,
  Digest,
  Enrichment,
  Filing,
  FilingsList,
  PdfAttachment,
  Report,
  ResearchNote,
  ResearchState,
  TranscriptMeta,
  WatchlistItem,
} from "./types";

// In the desktop (Electron) app the backend runs on a dynamic port that the
// shell injects as ?api=<port>. Next.js bakes rewrites() at build time, so the
// production proxy can't target a runtime port — instead we call the backend
// directly (its CORS allows any localhost origin). On the web/dev build there's no ?api param, so
// API_BASE is "" and requests stay same-origin and use the Next rewrite.
export function apiBase(): string {
  if (typeof window !== "undefined") {
    const p = new URLSearchParams(window.location.search).get("api");
    if (p && /^\d+$/.test(p)) return `http://127.0.0.1:${p}`;
  }
  return "";
}

export function apiUrl(path: string): string {
  return `${apiBase()}${path}`;
}

// Readable message for a failed response. FastAPI sends {"detail": "..."} for
// HTTPException but a list of {loc, msg, type} objects for 422 validation
// errors, which would otherwise surface as "[object Object]".
export async function errorDetail(res: Response): Promise<string> {
  const fallback = `${res.status} ${res.statusText}`.trim();
  let d: unknown;
  try {
    d = (await res.json())?.detail;
  } catch {
    return fallback; // non-JSON body (e.g. a proxy error page)
  }
  if (typeof d === "string") return d || fallback;
  if (Array.isArray(d)) {
    const msgs = d.map((x) => {
      if (x && typeof x === "object" && typeof x.msg === "string") {
        const loc = Array.isArray(x.loc)
          ? x.loc.filter((p: unknown) => p !== "body").join(".")
          : "";
        return loc ? `${loc}: ${x.msg}` : x.msg;
      }
      return typeof x === "string" ? x : JSON.stringify(x);
    });
    return msgs.join("; ") || fallback;
  }
  if (d && typeof d === "object") {
    const o = d as Record<string, unknown>;
    const m = o.message ?? o.msg ?? o.detail;
    return typeof m === "string" && m ? m : JSON.stringify(d);
  }
  return d == null ? fallback : String(d);
}

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(apiUrl(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await errorDetail(res));
  return res.json() as Promise<T>;
}

export async function fetchValuation(
  ticker: string,
  assumptions: Assumptions = {},
  refresh = false
): Promise<Report> {
  return postJSON<Report>("/api/valuation", {
    ticker,
    refresh,
    ...assumptions,
  });
}

export async function fetchEnrichment(ticker: string): Promise<Enrichment> {
  const res = await fetch(apiUrl(`/api/enrichment/${encodeURIComponent(ticker)}`));
  if (!res.ok) return { enabled: false };
  return res.json() as Promise<Enrichment>;
}

export async function postDigest(args: {
  report: Report | null;
  extra_context?: string;
  material_text?: string;
  pdfs?: PdfAttachment[];
}): Promise<Digest> {
  return postJSON<Digest>("/api/ai/digest", {
    report: args.report,
    extra_context: args.extra_context ?? "",
    material_text: args.material_text ?? "",
    pdfs: args.pdfs ?? [],
  });
}

export async function postChat(args: {
  report: Report | null;
  extra_context?: string;
  turns: ChatTurn[];
  pdfs?: PdfAttachment[];
}): Promise<string> {
  const r = await postJSON<{ reply: string }>("/api/ai/chat", {
    report: args.report,
    extra_context: args.extra_context ?? "",
    turns: args.turns,
    pdfs: args.pdfs ?? [],
  });
  return r.reply;
}

// --- filings & transcripts -------------------------------------------------- //
export async function fetchFilings(ticker: string): Promise<FilingsList> {
  const res = await fetch(apiUrl(`/api/filings/${encodeURIComponent(ticker)}`));
  if (!res.ok) throw new Error(await errorDetail(res));
  return res.json() as Promise<FilingsList>;
}

export async function digestFiling(args: {
  ticker: string;
  filing: Filing;
  report: Report | null;
  extra_context?: string;
}): Promise<{ digest: Digest; source: string }> {
  return postJSON("/api/filings/digest", {
    ticker: args.ticker,
    form: args.filing.form,
    filed: args.filing.filed,
    accession_number: args.filing.accession_number,
    primary_document: args.filing.primary_document,
    report: args.report,
    extra_context: args.extra_context ?? "",
  });
}

export async function fetchTranscripts(
  ticker: string
): Promise<{ enabled: boolean; transcripts: TranscriptMeta[] }> {
  const res = await fetch(
    apiUrl(`/api/transcripts/${encodeURIComponent(ticker)}`)
  );
  if (!res.ok) return { enabled: false, transcripts: [] };
  return res.json();
}

export async function digestTranscript(args: {
  ticker: string;
  year: number;
  quarter: number;
  report: Report | null;
  extra_context?: string;
}): Promise<{ digest: Digest; source: string }> {
  return postJSON("/api/transcripts/digest", {
    ticker: args.ticker,
    year: args.year,
    quarter: args.quarter,
    report: args.report,
    extra_context: args.extra_context ?? "",
  });
}

// --- research note & exports ------------------------------------------------ //
export async function postResearchNote(args: {
  report: Report | null;
  extra_context?: string;
  pdfs?: PdfAttachment[];
}): Promise<ResearchNote> {
  return postJSON<ResearchNote>("/api/ai/research_note", {
    report: args.report,
    extra_context: args.extra_context ?? "",
    pdfs: args.pdfs ?? [],
  });
}

export type ExportKind = "excel" | "html" | "memo" | "deck";

// Fallback names match the backend's own (used when the Content-Disposition
// header isn't readable, e.g. cross-origin in the desktop app).
const EXPORT_NAMES: Record<ExportKind, string> = {
  excel: "valuation.xlsx",
  html: "valuation.html",
  memo: "research_memo.docx",
  deck: "briefing.pptx",
};

function filenameFromDisposition(h: string | null): string | null {
  if (!h) return null;
  const star = /filename\*\s*=\s*[\w-]*'[^']*'([^;]+)/i.exec(h);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ""));
    } catch {
      /* fall through to the plain filename */
    }
  }
  const plain = /filename\s*=\s*(?:"([^"]+)"|([^;]+))/i.exec(h);
  const name = (plain?.[1] ?? plain?.[2] ?? "").trim();
  return name || null;
}

export async function downloadExport(
  kind: ExportKind,
  ticker: string,
  assumptions: Assumptions,
  note?: ResearchNote | null
): Promise<void> {
  const res = await fetch(apiUrl(`/api/export/${kind}`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ticker, note: note ?? null, ...assumptions }),
  });
  if (!res.ok) throw new Error(await errorDetail(res));
  const blob = await res.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download =
    filenameFromDisposition(res.headers.get("Content-Disposition")) ??
    `${ticker}_${EXPORT_NAMES[kind]}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
}

// --- watchlist & research persistence ---------------------------------------- //
export async function fetchWatchlist(): Promise<WatchlistItem[]> {
  const res = await fetch(apiUrl("/api/watchlist"));
  if (!res.ok) return [];
  return (await res.json()).watchlist ?? [];
}

export async function updateWatchlist(
  action: "add" | "remove",
  ticker: string,
  snapshot?: Partial<WatchlistItem>
): Promise<WatchlistItem[]> {
  const r = await postJSON<{ watchlist: WatchlistItem[] }>("/api/watchlist", {
    action,
    ticker,
    snapshot,
  });
  return r.watchlist;
}

export async function fetchResearchState(
  ticker: string
): Promise<ResearchState> {
  const res = await fetch(
    apiUrl(`/api/research_state/${encodeURIComponent(ticker)}`)
  );
  // A never-seen ticker is 200 {}; anything else is a failed read, and must
  // not look like an empty state (autosave would then wipe the saved one).
  if (!res.ok) throw new Error(`research state: ${await errorDetail(res)}`);
  return res.json();
}

export async function saveResearchState(
  ticker: string,
  state: ResearchState
): Promise<void> {
  await postJSON(`/api/research_state/${encodeURIComponent(ticker)}`, state);
}

// Read a File into base64 (strips the data: URL prefix) for PDF upload.
export function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result as string;
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}
