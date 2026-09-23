// Phase H — mirror of backend/models/execution_models.py (ExecutionStatusPanel).
// Read-only: the UI can display state but can NEVER enable, arm or submit.

export interface ExecutionStatusPanel {
  system_mode: string;
  live_execution_enabled: boolean;
  live_execution_armed: boolean;
  kill_switch: boolean;
  control_enabled: boolean;
  control_note: string;
  risk_state: string;
  risk_reason: string;
  broker: string;
  broker_verified: boolean;
  reconciliation: string;
  adapter: string;
  max_trades_per_day: number;
  max_open_positions: number;
  max_quantity: number;
  max_daily_loss: number;
  max_slippage_points: number;
  quantity: number;
  trades_today: number;
  open_orders: number;
  realized_pnl_today: number;
  last_block_reason: string;
  orders_submitted: number;
  confirmation_required: string[];
  note: string;
}
