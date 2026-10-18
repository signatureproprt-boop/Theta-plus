// Phase F — hand-written mirrors of backend/models/chart_models.py.
// Keep in sync with the Pydantic models in one edit.

export interface ChartSignalMarker {
  signal_id: string;
  timestamp: string; // canonical backend timestamp (Asia/Kolkata aware ISO)
  symbol: string;

  decision: string;
  state: string;
  marker_kind: string; // SETUP | INVALIDATION | WAIT
  side: string | null;

  spot: number | null;
  vwap: number | null; // BACKEND VWAP — strategy-authoritative
  vwap_distance: number | null;

  pcr: number | null;
  atm_pcr: number | null;
  pcr_trend: string | null;

  atm: number | null;
  ce_oi: number | null;
  ce_oi_change: number | null;
  pe_oi: number | null;
  pe_oi_change: number | null;

  score: number;
  ce_score: number;
  pe_score: number;
  max_score: number;

  reason: string;
  data_health: string;
  stale: boolean;

  strategy_version: string;
  feature_version: string;
  config_version: string;

  data_origin: string; // LIVE | REPLAY-SYNTHETIC
  data_origin_label: string;

  origin_signal_id: string | null;
  invalidation_timestamp: string | null;
  invalidation_reason: string | null;
}

export interface ChartSignalsResponse {
  symbol: string;
  tradingview_symbol: string;
  timeframe: string;
  timezone: string;

  data_origin: string;
  data_origin_label: string;
  include_wait: boolean;

  generated_at: string;
  count: number;
  markers: ChartSignalMarker[];

  latest_decision: string;
  latest_state: string;
  spot: number | null;
  vwap: number | null;
  vwap_distance: number | null;
  data_health: string;
  stale: boolean;

  notes: string[];
}

export type DataOrigin = "LIVE" | "REPLAY-SYNTHETIC";

// Display helper: backend timestamps are canonical; render them in IST.
export function fmtIst(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(d);
}

export function fmtIstDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    year: "numeric",
    month: "short",
    day: "2-digit",
  }).format(d);
}

export function num(v: number | null | undefined, digits = 2): string {
  return v === null || v === undefined ? "—" : v.toFixed(digits);
}

export function int(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : v.toLocaleString("en-IN");
}
