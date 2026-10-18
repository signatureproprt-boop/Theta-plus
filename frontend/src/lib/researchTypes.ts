// Phase I — mirrors backend/models/research_models.py (status + readiness + report).

export interface ChecklistItem {
  item: string;
  status: string; // PASS | FAIL | PENDING
  detail: string;
}

export interface ReadinessReport {
  generated_at: string;
  items: ChecklistItem[];
  passed: number;
  failed: number;
  pending: number;
  optional: number;
  mandatory_gates: string[];
  mandatory_pending: string[];
  telegram_required: boolean;
  production_ready: boolean;
  live_execution_enabled: boolean;
  live_execution_armed: boolean;
  note: string;
}

export interface DatasetDescriptor {
  dataset_id: string;
  dataset_hash: string;
  source: string;
  data_origin: string;
  date_from: string;
  date_to: string;
  trading_days: number;
  snapshot_count: number;
  created_at: string;
  schema_version: string;
  feature_version: string;
  strategy_version: string;
  note: string;
}

export interface DataQualitySummary {
  production_quality: boolean;
  snapshot_count: number;
  trading_days: number;
  duplicate_snapshots: number;
  out_of_order_snapshots: number;
  missing_oi: number;
  missing_ltp: number;
  invalid_values: number;
  failures: string[];
}

export interface DatasetStatus {
  real_historical_configured: boolean;
  real_historical_path_present: boolean;
  real_historical_status: string;
  live_real_data_status: string;
  dhan_credentials_present: boolean;
  instrument_map_present: boolean;
  dataset: DatasetDescriptor | null;
  quality: DataQualitySummary | null;
  note: string;
}

export interface ResearchAnalytics {
  sample_size_resolved: number;
  sample_size_signals: number;
  gross_points_total: number | null;
  net_points_total: number | null;
  max_drawdown_points: number | null;
  avg_outcome_points: number | null;
  profit_factor: number | null;
  note: string;
}

export interface ValidationReport {
  dataset: DatasetDescriptor;
  quality: DataQualitySummary;
  leakage: { passed: boolean; detail: string };
  session_reset: { passed: boolean; detail: string };
  determinism: { passed: boolean; replay_hash: string };
  analytics: ResearchAnalytics;
  metrics: Record<string, unknown>;
  replay_hash: string;
  data_origin: string;
  notes: string[];
}
