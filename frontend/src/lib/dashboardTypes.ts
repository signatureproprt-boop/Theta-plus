// Mirror of backend/models/dashboard_models.py — keep in sync in the same edit.
// Scores are X/100 integers; NEVER percentages or probabilities.

export interface SystemInfo {
  enabled: boolean;
  strategy_version: string;
  config_version: string;
  feature_engine_version: string;
  data_source: string; // sim | dhan
}

export interface MarketPanel {
  index: string;
  spot: number | null;
  vwap: number | null;
  vwap_distance: number | null;
  vwap_distance_percent: number | null;
  above_vwap: boolean;
  below_vwap: boolean;
  atm: number | null;
  last_update: string | null;
}

export interface PcrPanel {
  total_pcr: number | null;
  atm_pcr: number | null;
  pcr_change: number | null;
  atm_pcr_change: number | null;
  pcr_trend: string;
  atm_pcr_trend: string;
}

export interface OiPanel {
  put_oi: number | null;
  call_oi: number | null;
  put_oi_change: number | null;
  call_oi_change: number | null;
}

export interface SignalPanel {
  decision: string; // "CE SETUP" | "PE SETUP" | "WAIT" — never subjective labels
  decision_code: string;
  ce_score: number;
  pe_score: number;
  ce_max_score: number;
  pe_max_score: number;
  state: string;
  signal_time: string | null;
  cooldown_until: string | null;
  reasons: string[];
  confirmed: string[];
  unavailable: string[];
}

export interface OptionPanel {
  atm_strike: number | null;
  atm_ce_ltp: number | null;
  atm_pe_ltp: number | null;
  ce_oi: number | null;
  pe_oi: number | null;
  ce_oi_change: number | null;
  pe_oi_change: number | null;
  ce_iv: number | null;
  pe_iv: number | null;
}

export interface HealthItem {
  component: string;
  status: string; // OK | WARNING | STALE | ERROR | DISABLED
  detail: string;
}

export interface DataHealthPanel {
  overall: string;
  data_age_seconds: number | null;
  items: HealthItem[];
}

export interface SettingsPanel {
  index: string;
  market_open: string;
  signal_start: string;
  signal_end: string;
  atm_range: number;
  pcr_lookback: number;
  min_score: number;
  target_points: number;
  stoploss_points: number;
  signal_cooldown: number;
  system_enabled: boolean;
  timezone: string;
  strategy_version: string;
  feature_engine_version: string;
}

export interface SystemLogEntry {
  timestamp: string;
  level: string; // INFO | WARN | ERROR
  component: string;
  event: string;
  message: string;
}

export interface SheetsStatusPanel {
  enabled: boolean;
  configured: boolean;
  last_flush_at: string | null;
  last_error: string;
  rows_written: number;
}

export interface DashboardPayload {
  generated_at: string;
  system: SystemInfo;
  market: MarketPanel;
  pcr: PcrPanel;
  oi: OiPanel;
  signal: SignalPanel;
  option: OptionPanel;
  data_health: DataHealthPanel;
  settings: SettingsPanel;
  sheets: SheetsStatusPanel;
  system_log: SystemLogEntry[];
}
