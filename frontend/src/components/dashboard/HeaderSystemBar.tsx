import { Badge } from "@/components/ui/badge";
import { StatusDot, fmtTime } from "./shared";
import type { DashboardPayload } from "@/lib/dashboardTypes";

export default function HeaderSystemBar({
  data,
  isError,
}: {
  data?: DashboardPayload;
  isError: boolean;
}) {
  const enabled = data?.system.enabled;
  const systemStatus = enabled === undefined ? "DISABLED" : enabled ? "OK" : "ERROR";
  return (
    <header className="flex flex-col md:flex-row items-start md:items-center justify-between gap-4 rounded-lg border border-[#1F2937] bg-[#11161D] px-5 py-3.5">
      <div className="flex items-center gap-3">
        <h1
          data-testid="app-title"
          className="font-mono text-lg font-semibold tracking-tight text-[#F0F4F8] drop-shadow-[0_0_12px_rgba(16,185,129,0.25)]"
        >
          PCR TRADING SYSTEM
        </h1>
        <Badge
          variant="outline"
          data-testid="index-badge"
          className="border-[#1F2937] font-mono text-[10px] text-[#38BDF8]"
        >
          {data?.market.index ?? "NIFTY"}
        </Badge>
        {isError && (
          <span
            data-testid="backend-unavailable-pill"
            className="font-mono text-[10px] uppercase tracking-[0.15em] text-rose-400"
          >
            backend unavailable
          </span>
        )}
      </div>
      <div className="flex items-center gap-4">
        <span className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.15em] text-[#8A99A8]">
          system
          <span
            data-testid="system-status-pill"
            className={`flex items-center gap-1.5 rounded px-2 py-0.5 font-mono text-[11px] ${
              enabled ? "bg-[#052E1B] text-[#34D399]" : "bg-[#360C14] text-[#F87171]"
            }`}
          >
            <StatusDot status={systemStatus} testid="system-status-dot" />
            {enabled ? "ENABLED" : "DISABLED"}
          </span>
        </span>
        <span className="font-mono text-[11px] text-[#8A99A8]">
          strategy <span data-testid="header-strategy-version" className="text-[#F0F4F8]">{data?.system.strategy_version ?? "—"}</span>
        </span>
        <span className="font-mono text-[11px] text-[#8A99A8]">
          last update <span data-testid="header-last-update" className="text-[#F0F4F8]">{fmtTime(data?.market.last_update)}</span>
        </span>
      </div>
    </header>
  );
}
