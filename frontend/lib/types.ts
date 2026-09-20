// TypeScript mirror of backend/serialization.py (which mirrors
// equity_valuation/schemas.py). This is the binding contract between the
// FastAPI backend and the UI — keep field names identical to the engine.

export interface MarketData {
  ticker: string;
  name: string;
  currency: string;
  price: number;
  shares_outstanding: number;
  market_cap: number;
  beta: number | null;
  dividend_per_share: number | null;
  fifty_two_week_low: number | null;
  fifty_two_week_high: number | null;
  sector: string | null;
  industry: string | null;
}

export interface AnnualFinancials {
  fiscal_years: number[];
  revenue: number[];
  ebit: number[];
  ebitda: number[];
  net_income: number[];
  dep_amort: number[];
  capex: number[];
  change_in_nwc: number[];
  interest_expense: number[];
  tax_expense: number[];
  pretax_income: number[];
  dividends_paid: number[];
  diluted_shares: number[];
}

export interface BalanceSheet {
  as_of: string;
  total_debt: number;
  cash_and_investments: number;
  total_equity: number;
  minority_interest: number;
  preferred_equity: number;
  net_debt: number;
}

export interface Company {
  ticker: string;
  name: string;
  cik: string | null;
  financials: AnnualFinancials;
  balance_sheet: BalanceSheet;
  market: MarketData;
  source_notes: string[];
}

export interface Macro {
  risk_free_rate: number;
  equity_risk_premium: number;
  tax_rate: number | null;
  pretax_cost_of_debt: number | null;
}

export interface WACC {
  cost_of_equity: number;
  after_tax_cost_of_debt: number;
  weight_equity: number;
  weight_debt: number;
  wacc: number;
  beta: number;
  detail: Record<string, unknown>;
}

export interface DCF {
  wacc: WACC;
  years: number[];
  revenue: number[];
  ebit: number[];
  nopat: number[];
  fcff: number[];
  discount_factors: number[];
  pv_fcff: number[];
  terminal_value: number;
  pv_terminal: number;
  enterprise_value: number;
  net_debt: number;
  equity_value: number;
  shares: number;
  implied_price: number;
  current_price: number;
  upside: number;
  assumptions: Record<string, unknown>;
}

export interface CompRow {
  ticker: string;
  name: string;
  market_cap: number | null;
  enterprise_value: number | null;
  ev_ebitda: number | null;
  ev_sales: number | null;
  pe: number | null;
  pb: number | null;
  peg: number | null;
}

export interface StatRow {
  median: number | null;
  mean: number | null;
  min: number | null;
  max: number | null;
  p25: number | null;
  p75: number | null;
  n?: number;
}

export interface Comps {
  target: CompRow;
  peers: CompRow[];
  stats: Record<string, StatRow>;
  implied: Record<string, number | null>;
  implied_price_summary: {
    low?: number | null;
    median?: number | null;
    high?: number | null;
  };
  notes: string[];
}

export interface DDM {
  method: string;
  implied_price: number;
  cost_of_equity: number;
  detail: Record<string, unknown>;
}

export interface FCFE {
  years: number[];
  fcfe: number[];
  pv_fcfe: number[];
  terminal_value: number;
  pv_terminal: number;
  equity_value: number;
  shares: number;
  implied_price: number;
  current_price: number;
  cost_of_equity: number;
  detail: Record<string, unknown>;
}

export interface Sensitivity {
  title: string;
  row_label: string;
  col_label: string;
  row_values: number[];
  col_values: number[];
  grid: (number | null)[][];
}

export interface FootballRow {
  method: string;
  low: number;
  base: number;
  high: number;
}

export interface Summary {
  ticker: string;
  name: string;
  currency: string;
  current_price: number;
  methods: Record<string, number>;
  blended_target: number | null;
  blended_upside: number | null;
  recommendation: string;
}

export interface ReverseDCF {
  converged: boolean;
  implied_growth_y1: number | null;
  current_assumption_y1?: number | null;
  note?: string;
}

export interface Report {
  summary: Summary;
  company: Company;
  macro: Macro;
  current_price: number;
  dcf: DCF | null;
  comps: Comps | null;
  ddm: DDM | null;
  fcfe: FCFE | null;
  sensitivities: Sensitivity[];
  football_field: FootballRow[];
  warnings: string[];
  assumptions_used: AssumptionsUsed;
  reverse_dcf?: ReverseDCF | null;
}

// --- FMP enrichment ------------------------------------------------------- //
export interface NewsItem {
  title: string;
  text: string;
  site: string;
  url: string;
  image?: string;
  publishedDate: string;
  symbol?: string;
}

export interface Enrichment {
  enabled: boolean;
  profile?: Record<string, unknown> | null;
  quote?: Record<string, unknown> | null;
  ratios_ttm?: Record<string, unknown> | null;
  key_metrics_ttm?: Record<string, unknown> | null;
  price_target?: Record<string, unknown> | null;
  rating?: Record<string, unknown> | null;
  peers?: string[];
  estimates?: Record<string, unknown>[];
  news?: NewsItem[];
}

// --- Editable assumptions (sent to /api/valuation) ------------------------ //
export interface Assumptions {
  rf?: number; // risk-free rate (decimal)
  erp?: number; // equity risk premium (decimal)
  tax_rate?: number; // decimal; null => engine derives effective
  forecast_years?: number;
  terminal_growth?: number; // decimal
  terminal_method?: "gordon" | "exit_multiple";
  exit_ev_ebitda?: number; // multiple
  target_ebit_margin?: number; // decimal
  revenue_growth_y1?: number; // decimal; near-term growth, fades to terminal
  peers?: string; // comma-separated tickers
}

export type AssumptionsUsed = Assumptions & {
  cost_of_debt?: number | null;
  revenue_growth?: number[] | null;
};

// --- AI researcher -------------------------------------------------------- //
export type AssumptionField =
  | "terminal_growth"
  | "forecast_years"
  | "target_ebit_margin"
  | "risk_free_rate"
  | "equity_risk_premium"
  | "tax_rate"
  | "exit_ev_ebitda"
  | "revenue_growth_y1";

export interface AssumptionSuggestion {
  field: AssumptionField;
  label: string;
  current_value: number | null;
  suggested_value: number;
  unit: "percent" | "number" | "years" | "multiple";
  rationale: string;
}

export interface CitedFact {
  fact: string;
  source: string;
}

export interface Digest {
  summary: string;
  sentiment: "bullish" | "bearish" | "neutral" | "mixed";
  key_facts: CitedFact[];
  risks: string[];
  catalysts: string[];
  suggested_assumptions: AssumptionSuggestion[];
}

// A digest plus where it came from (pasted text, a named filing, a call…).
export interface DigestEntry {
  source: string;
  digest: Digest;
  at?: string;
}

export interface Citation {
  source: string;
  note: string;
}

export interface ResearchNote {
  title: string;
  stance: "constructive" | "cautious" | "balanced";
  executive_summary: string;
  thesis: string[];
  valuation_view: string;
  key_drivers: string[];
  risks: string[];
  red_flags: string[];
  catalysts: string[];
  what_would_change_my_mind: string[];
  citations: Citation[];
}

export interface PdfAttachment {
  name: string;
  data_base64: string;
}

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

// --- SEC filings & transcripts --------------------------------------------- //
export interface Filing {
  form: string;
  filed: string;
  report_date: string;
  accession_number: string;
  primary_document: string;
  description: string;
  url: string;
}

export interface FilingsList {
  ticker: string;
  cik: string;
  name: string;
  filings: Filing[];
}

export interface TranscriptMeta {
  quarter: number;
  year: number;
  date: string;
}

// --- watchlist & persisted research ---------------------------------------- //
export interface WatchlistItem {
  ticker: string;
  name: string | null;
  currency: string | null;
  price: number | null;
  blended_target: number | null;
  recommendation: string | null;
  updated_at: string;
}

export interface ResearchState {
  notes?: string;
  digests?: DigestEntry[];
  note?: ResearchNote | null;
  assumptions?: Assumptions | null;
  updated_at?: string;
}
