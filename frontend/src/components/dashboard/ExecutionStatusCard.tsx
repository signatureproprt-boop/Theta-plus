import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/lib/api";
import { Panel } from "./shared";
import type { ExecutionStatusPanel } from "@/lib/executionTypes";

// Phase H — execution status is DISPLAY ONLY. There is deliberately no enable,
// arm or submit control: arming is a server-side operator action.

function Flag({ label, value, danger, testid }: {
  label: string; value: string; danger?: boolean; testid: string;
}) {
  return (
    <div className="rounded border border-[#1F2937] bg-[#0D1117] px-3 py-2">
      <p className="font-mono text-[9px] uppercase tracking-[0.18em] text-[#4B5563]">{label}</p>
      <p
        data-testid={testid}
        className={`mt-1 font-mono text-[12px] font-semibold ${danger ? "text-[#F87171]" : "text-[#8AE6A2]"}`}
      >
        {value}
      </p>
    </div>
  );
}

export default function ExecutionStatusCard() {
  const { data, isError } = useQuery({
    queryKey: ["execution-status"],
    queryFn: () => apiGet<ExecutionStatusPanel>("/execution/status"),
    refetchInterval: 10000,
  });

  const live = data?.system_mode === "LIVE";

  return (
    <Panel
      title="execution / risk layer"
      action={
        <span
          data-testid="execution-mode-badge"
          className="rounded border border-[#1F2937] bg-[#11161D] px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.18em] text-[#8A99A8]"
        >
          {data?.system_mode ?? "—"}
        </span>
      }
      testid="execution-status-card"
    >
      {isError && (
        <p data-testid="execution-status-error" className="font-mono text-[11px] text-[#F87171]">
          execution status unavailable
        </p>
      )}

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <Flag
          label="live execution"
          value={data?.live_execution_enabled ? "ENABLED" : "DISABLED"}
          danger={!!data?.live_execution_enabled}
          testid="execution-enabled-flag"
        />
        <Flag
          label="armed"
          value={data?.live_execution_armed ? "YES" : "NO"}
          danger={!!data?.live_execution_armed}
          testid="execution-armed-flag"
        />
        <Flag
          label="kill switch"
          value={data?.kill_switch ? "ON" : "OFF"}
          testid="execution-kill-switch-flag"
        />
        <Flag
          label="risk"
          value={data?.risk_state ?? "—"}
          danger={data?.risk_state === "BLOCKED"}
          testid="execution-risk-flag"
        />
        <Flag
          label="broker"
          value={data?.broker ?? "—"}
          danger={data?.broker === "ERROR"}
          testid="execution-broker-flag"
        />
        <Flag
          label="reconciliation"
          value={data?.reconciliation ?? "—"}
          danger={data?.reconciliation === "MISMATCH"}
          testid="execution-reconciliation-flag"
        />
      </div>

      <div className="mt-3 grid grid-cols-2 gap-x-6 gap-y-1 font-mono text-[11px] sm:grid-cols-4">
        {[
          ["adapter", data?.adapter ?? "—", "execution-adapter"],
          ["quantity", String(data?.quantity ?? "—"), "execution-quantity"],
          ["max trades/day", String(data?.max_trades_per_day ?? "—"), "execution-max-trades"],
          ["max open pos", String(data?.max_open_positions ?? "—"), "execution-max-positions"],
          ["max qty", String(data?.max_quantity ?? "—"), "execution-max-quantity"],
          ["max daily loss", String(data?.max_daily_loss ?? "—"), "execution-max-daily-loss"],
          ["max slippage", String(data?.max_slippage_points ?? "—"), "execution-max-slippage"],
          ["submissions", String(data?.orders_submitted ?? 0), "execution-submissions"],
        ].map(([label, value, testid]) => (
          <div key={label} className="flex items-baseline justify-between gap-2">
            <span className="text-[#4B5563]">{label}</span>
            <span data-testid={testid} className="text-[#D6DEE7]">{value}</span>
          </div>
        ))}
      </div>

      {!!data?.last_block_reason && (
        <p data-testid="execution-last-block" className="mt-2 font-mono text-[10px] text-[#E9C46A]">
          last block: {data.last_block_reason}
        </p>
      )}

      <p
        data-testid="execution-control-note"
        className="mt-3 rounded border border-[#1F2937] bg-[#0D1117] px-3 py-2 font-sans text-[10px] leading-relaxed text-[#4B5563]"
      >
        {data?.control_note ?? "LIVE EXECUTION CONTROL = DISABLED"}
      </p>

      <details className="mt-2">
        <summary
          data-testid="execution-confirmation-summary"
          className="cursor-pointer font-mono text-[10px] uppercase tracking-[0.18em] text-[#8A99A8]"
        >
          pre-arm confirmation checklist
        </summary>
        <ul data-testid="execution-confirmation-list" className="mt-2 space-y-0.5 font-mono text-[10px] text-[#8A99A8]">
          {(data?.confirmation_required ?? []).map((line) => (
            <li key={line}>• {line}</li>
          ))}
        </ul>
        <p className="mt-2 font-sans text-[10px] text-[#4B5563]">
          {data?.note ?? ""}
        </p>
      </details>

      {live && !data?.broker_verified && (
        <p data-testid="execution-broker-pending" className="mt-2 font-mono text-[10px] text-[#E9C46A]">
          real broker verification PENDING — no live order can be placed
        </p>
      )}
    </Panel>
  );
}
