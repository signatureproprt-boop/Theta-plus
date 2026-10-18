// Phase G — hand-written mirrors of backend/models/paper_models.py and
// backend/models/alert_models.py. Keep in sync with the Pydantic models.

export interface PaperPosition {
  paper_position_id: string;
  signal_id: string;
  timestamp: string;
  underlying: string;
  option_type: string; // CE | PE
  strike: number | null;

  entry_price: number | null;
  current_price: number | null;
  target_price: number | null;
  stoploss_price: number | null;
  quantity: number;

  state: string; // OPEN | TARGET | STOPLOSS | INVALIDATED | EXPIRED | NO_DATA | AMBIGUOUS
  exit_price: number | null;
  exit_time: string | null;
  exit_reason: string;

  gross_points: number | null;
  gross_pnl: number | null;
  assumed_slippage_points: number;
  costs: number;
  net_pnl: number | null;

  data_origin: string;
  data_origin_label: string;
  mode: string;
  label: string;

  strategy_version: string;
  feature_version: string;
}

export interface PaperSummary {
  mode: string;
  paper_trading_enabled: boolean;
  label: string;
  data_origin: string;
  data_origin_label: string;

  total_positions: number;
  open_positions: number;
  target: number;
  stoploss: number;
  invalidated: number;
  expired: number;
  ambiguous: number;
  no_data: number;

  gross_points: number;
  gross_pnl: number;
  costs: number;
  net_pnl: number;

  quantity: number;
  target_points: number;
  stoploss_points: number;
  max_open_positions: number;
  cost_note: string;
  open_position: PaperPosition | null;
}

export interface AlertProviderStatus {
  provider: string;
  configured: boolean;
  status: string; // CONFIGURED | NOT_CONFIGURED | DISABLED
  last_error: string;
  sent: number;
  failed: number;
  suppressed_duplicates: number;
  note: string;
  delivery: string; // CONFIGURED | PENDING
  real_delivery_verified: boolean;
}
