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
// directly (its CORS is open). On the web/dev build there's no ?api param, so
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

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(apiUrl(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (j?.detail) detail = j.detail;
    } catch {
      /* keep status text */
    }
    throw new Error(detail);
  }
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
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      detail = (await res.json())?.detail ?? detail;
    } catch {}
    throw new Error(detail);
  }
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
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      detail = (await res.json())?.detail ?? detail;
    } catch {}
    throw new Error(detail);
  }
  const blob = await res.blob();
  const ext = { excel: "xlsx", html: "html", memo: "docx", deck: "pptx" }[kind];
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${ticker}_${kind === "excel" ? "valuation" : kind}.${ext}`;
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
  if (!res.ok) return {};
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
