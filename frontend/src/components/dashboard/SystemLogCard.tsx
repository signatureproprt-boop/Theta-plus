import { Panel } from "./shared";
import { cn } from "@/lib/utils";
import type { SystemLogEntry } from "@/lib/dashboardTypes";

const LEVEL_TONE: Record<string, string> = {
  INFO: "text-[#38BDF8]",
  WARN: "text-[#FBBF24]",
  ERROR: "text-[#F87171]",
};

export default function SystemLogCard({ entries }: { entries?: SystemLogEntry[] }) {
  return (
    <Panel
      title="System Log"
      testid="system-log-card"
      action={
        <span className="font-mono text-[10px] text-[#8A99A8]" data-testid="system-log-count">
          {entries ? `${entries.length} entries` : ""}
        </span>
      }
    >
      {entries === undefined ? (
        <div className="font-mono text-xs text-[#8A99A8]">log unavailable</div>
      ) : entries.length === 0 ? (
        <div className="font-mono text-xs text-[#8A99A8]">no events recorded yet</div>
      ) : (
        <ul data-testid="system-log-list" className="max-h-56 space-y-1 overflow-y-auto pr-1">
          {entries
            .slice()
            .reverse()
            .map((e, i) => (
              <li key={`${e.timestamp}-${i}`} data-testid="system-log-entry" className="flex gap-2 font-mono text-[11px] leading-relaxed">
                <span className="text-[#64748B]">{new Date(e.timestamp).toLocaleTimeString("en-IN", { hour12: false, timeZone: "Asia/Kolkata" })}</span>
                <span className={cn("w-10 shrink-0", LEVEL_TONE[e.level] ?? "text-[#8A99A8]")}>{e.level}</span>
                <span className="text-[#64748B] w-24 shrink-0 truncate">{e.event}</span>
                <span className="text-[#E2E8F0] truncate">{e.message}</span>
              </li>
            ))}
        </ul>
      )}
    </Panel>
  );
}
