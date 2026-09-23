import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/lib/api";
import { Panel } from "./shared";
import { cn } from "@/lib/utils";
import { num } from "@/lib/chartTypes";
import type { AlertProviderStatus, PaperSummary } from "@/lib/paperTypes";

// Phase G — PAPER / HYPOTHETICAL research section. No execution control exists:
// this card only reports what the paper engine recorded downstream of the
// frozen signal engine.
const STATE_TONE: Record<string, string> = {
  OPEN: "text-sky-400",
  TARGET: "text-emerald-400",
  STOPLOSS: "text-red-400",
  INVALIDATED: "text-amber-400",
  EXPIRED: "text-slate-400",
  AMBIGUOUS: "text-amber-400",
  NO_DATA: "text-slate-500",
};

function Cell({ label, value, testid, tone }: { label: string; value: string; testid: string; tone?: string }) {
  return (
    <div className="min-w-0">
      <span className="block font-sans text-[10px] uppercase tracking-[0.15em] text-[#4B5563]">{label}</span>
      <span data-testid={testid} className={cn("mt-0.5 block truncate font-mono text-xs text-[#F0F4F8]", tone)}>
        {value}
      </span>
    </div>
  );
}

export default function PaperTradingCard() {
  const { data, isError } = useQuery({
    queryKey: ["paper-summary"],
    queryFn: () => apiGet<PaperSummary>("/paper/summary"),
    refetchInterval: 5000,
  });
  const { data: alerts } = useQuery({
    queryKey: ["alerts-status"],
    queryFn: () => apiGet<AlertProviderStatus>("/alerts/status"),
    refetchInterval: 15000,
  });

  const open = data?.open_position ?? null;

  return (
    <Panel
      title="Paper Trading (Paper / Hypothetical)"
      testid="paper-trading-card"
      action={
        <span
          data-testid="paper-mode-badge"
          className="rounded border border-[#1F2937] px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.15em] text-[#7DD3FC]"
        >
          {data?.mode ?? "RESEARCH"} MODE
        </span>
      }
    >
      {isError ? (
        <p data-testid="paper-unavailable" className="font-mono text-[11px] uppercase tracking-[0.15em] text-amber-400">
          paper data unavailable
        </p>
      ) : (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-8">
            <Cell label="State" value={open?.state ?? (data?.open_positions ? "OPEN" : "—")}
                  testid="paper-open-state" tone={STATE_TONE[open?.state ?? ""] ?? ""} />
            <Cell label="Option" value={open ? `${open.strike ?? "—"} ${open.option_type}` : "—"} testid="paper-open-option" />
            <Cell label="Entry" value={num(open?.entry_price ?? null)} testid="paper-open-entry" />
            <Cell label="Current" value={num(open?.current_price ?? null)} testid="paper-open-current" />
            <Cell label="Target" value={num(open?.target_price ?? null)} testid="paper-open-target" />
            <Cell label="SL" value={num(open?.stoploss_price ?? null)} testid="paper-open-stoploss" />
            <Cell
              label="Points"
              value={
                open && open.entry_price !== null && open.current_price !== null
                  ? (open.current_price - open.entry_price).toFixed(2)
                  : "—"
              }
              testid="paper-open-points"
            />
            <Cell label="Qty" value={String(data?.quantity ?? 1)} testid="paper-open-quantity" />
          </div>

          <div className="grid grid-cols-2 gap-3 border-t border-[#1F2937] pt-3 sm:grid-cols-3 lg:grid-cols-6">
            <Cell label="Paper Trades" value={String(data?.total_positions ?? 0)} testid="paper-total-positions" />
            <Cell label="Open" value={String(data?.open_positions ?? 0)} testid="paper-open-count" />
            <Cell label="Targets" value={String(data?.target ?? 0)} testid="paper-target-count" />
            <Cell label="Stops" value={String(data?.stoploss ?? 0)} testid="paper-stoploss-count" />
            <Cell label="Invalidations" value={String(data?.invalidated ?? 0)} testid="paper-invalidated-count" />
            <Cell
              label="Net P&L (paper)"
              value={num(data?.net_pnl ?? null)}
              testid="paper-net-pnl"
              tone={(data?.net_pnl ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}
            />
          </div>

          <div className="space-y-1 border-t border-[#1F2937] pt-3">
            <p data-testid="paper-label" className="font-mono text-[10px] uppercase tracking-[0.12em] text-amber-400/90">
              {data?.label ?? "PAPER / HYPOTHETICAL — not broker execution"}
            </p>
            <p data-testid="paper-cost-note" className="font-sans text-[10px] leading-relaxed text-[#4B5563]">
              {data?.cost_note ?? ""} {data?.data_origin_label ?? ""}
            </p>
            <p data-testid="alert-provider-status" className="font-sans text-[10px] leading-relaxed text-[#4B5563]">
              Telegram alerts: {alerts?.status ?? "NOT_CONFIGURED"} · delivery {alerts?.delivery ?? "PENDING"} · sent{" "}
              {alerts?.sent ?? 0} · failed {alerts?.failed ?? 0} · duplicates suppressed{" "}
              {alerts?.suppressed_duplicates ?? 0}
            </p>
          </div>
        </div>
      )}
    </Panel>
  );
}
