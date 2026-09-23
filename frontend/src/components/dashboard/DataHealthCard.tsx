import { Panel, SkeletonRows, StatusDot, fmtNum } from "./shared";
import { cn } from "@/lib/utils";
import type { DataHealthPanel as Health, HealthItem } from "@/lib/dashboardTypes";

function HealthRow({ item }: { item: HealthItem }) {
  const slug = item.component.toLowerCase().replace(/\s+/g, "-");
  return (
    <div
      data-testid={`health-item-${slug}`}
      className="flex items-center justify-between rounded border border-[#1F2937] bg-[#0C1017] px-3 py-1.5"
    >
      <span className="font-sans text-xs text-[#F0F4F8]">{item.component}</span>
      <span className="flex items-center gap-2">
        {item.detail && <span className="font-mono text-[10px] text-[#8A99A8]">{item.detail}</span>}
        <StatusDot status={item.status} testid={`health-dot-${slug}`} />
        <span
          data-testid={`health-status-${slug}`}
          className={cn(
            "font-mono text-[11px]",
            item.status === "OK" && "text-emerald-400",
            item.status === "WARNING" && "text-amber-400",
            item.status === "STALE" && "text-red-400",
            item.status === "ERROR" && "text-rose-400",
            item.status === "DISABLED" && "text-slate-500",
          )}
        >
          {item.status}
        </span>
      </span>
    </div>
  );
}

export default function DataHealthCard({ health }: { health?: Health }) {
  return (
    <Panel title="Data Health" testid="data-health-card">
      {health ? (
        <div className="space-y-2">
          <div className="flex items-center justify-between rounded border border-[#1F2937] bg-[#0C1017] px-3 py-2">
            <span className="font-sans text-xs uppercase tracking-[0.15em] text-[#8A99A8]">Overall</span>
            <span className="flex items-center gap-2">
              <span className="font-mono text-[10px] text-[#8A99A8]">
                data age {health.data_age_seconds != null ? `${fmtNum(health.data_age_seconds, 1)} sec` : "—"}
              </span>
              <StatusDot status={health.overall} testid="health-overall-dot" />
              <span
                data-testid="health-overall-pill"
                className={cn(
                  "font-mono text-xs",
                  health.overall === "OK" && "text-emerald-400",
                  health.overall === "WARNING" && "text-amber-400",
                  health.overall === "STALE" && "text-red-400",
                  health.overall === "ERROR" && "text-rose-400",
                  health.overall === "DISABLED" && "text-slate-500",
                )}
              >
                {health.overall}
              </span>
            </span>
          </div>
          {health.items.map((item) => (
            <HealthRow key={item.component} item={item} />
          ))}
        </div>
      ) : (
        <SkeletonRows rows={5} />
      )}
    </Panel>
  );
}
