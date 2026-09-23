import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/lib/api";
import { fmtIst, num } from "@/lib/chartTypes";
import type { PaperPosition } from "@/lib/paperTypes";

// Phase G — paper entry / target / SL / exit levels for the selected signal.
// Read-only: the chart has no execution control and no strategy authority.
export default function PaperLevelsPanel({ signalId }: { signalId?: string }) {
  const { data, isError } = useQuery({
    queryKey: ["paper-positions"],
    queryFn: () => apiGet<PaperPosition[]>("/paper/positions"),
    refetchInterval: 5000,
  });

  const position = signalId ? data?.find((p) => p.signal_id === signalId) : undefined;

  return (
    <div data-testid="paper-levels-panel" className="rounded-lg border border-[#1F2937] bg-[#11161D]">
      <header className="flex items-center justify-between border-b border-[#1F2937] px-4 py-2.5">
        <h2 className="font-mono text-[11px] uppercase tracking-[0.15em] text-[#8A99A8]">
          Paper Levels (Paper / Hypothetical)
        </h2>
        <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-amber-400/90">no execution</span>
      </header>
      <div className="p-4">
        {isError ? (
          <p data-testid="paper-levels-unavailable" className="font-mono text-[11px] uppercase tracking-[0.15em] text-amber-400">
            paper data unavailable
          </p>
        ) : !position ? (
          <p data-testid="paper-levels-empty" className="font-mono text-[11px] uppercase tracking-[0.15em] text-[#4B5563]">
            no paper position for this signal
          </p>
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            {[
              { label: "Option", value: `${position.strike ?? "—"} ${position.option_type}`, testid: "paper-level-option" },
              { label: "Entry", value: num(position.entry_price), testid: "paper-level-entry" },
              { label: "Target", value: num(position.target_price), testid: "paper-level-target" },
              { label: "SL", value: num(position.stoploss_price), testid: "paper-level-stoploss" },
              { label: "Exit", value: num(position.exit_price), testid: "paper-level-exit" },
              { label: "State", value: position.state, testid: "paper-level-state" },
            ].map((tile) => (
              <div key={tile.testid} className="min-w-0">
                <span className="block font-sans text-[10px] uppercase tracking-[0.15em] text-[#4B5563]">
                  {tile.label}
                </span>
                <span data-testid={tile.testid} className="mt-0.5 block truncate font-mono text-xs text-[#F0F4F8]">
                  {tile.value}
                </span>
              </div>
            ))}
            <div className="col-span-2 sm:col-span-3 lg:col-span-6">
              <span data-testid="paper-level-meta" className="font-sans text-[10px] text-[#4B5563]">
                {position.label} · net P&L {num(position.net_pnl)} · {position.data_origin_label}
                {position.exit_time ? ` · exit ${fmtIst(position.exit_time)} IST` : ""}
              </span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
