import { cn } from "@/lib/utils";
import type { DataOrigin } from "@/lib/chartTypes";

// Phase F controls: NIFTY only, 5m only, data origin, marker visibility.
// No indicator or strategy controls — Phase F is visualization only.
export default function ChartControls({
  origin,
  onOrigin,
  includeWait,
  onIncludeWait,
}: {
  origin: DataOrigin;
  onOrigin: (o: DataOrigin) => void;
  includeWait: boolean;
  onIncludeWait: (v: boolean) => void;
}) {
  const btn = (active: boolean) =>
    cn(
      "rounded border px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.15em] transition-colors duration-150",
      active
        ? "border-[#38BDF8] bg-[#082F49] text-[#7DD3FC]"
        : "border-[#1F2937] bg-[#11161D] text-[#8A99A8] hover:text-[#E2E8F0]",
    );

  return (
    <div
      data-testid="chart-controls"
      className="flex flex-wrap items-center gap-x-6 gap-y-3 rounded-lg border border-[#1F2937] bg-[#11161D] px-4 py-3"
    >
      <div className="flex items-center gap-2">
        <span className="font-sans text-[10px] uppercase tracking-[0.15em] text-[#4B5563]">Symbol</span>
        <span data-testid="chart-symbol-value" className="font-mono text-xs text-[#F0F4F8]">
          NIFTY
        </span>
      </div>
      <div className="flex items-center gap-2">
        <span className="font-sans text-[10px] uppercase tracking-[0.15em] text-[#4B5563]">Timeframe</span>
        <span data-testid="chart-timeframe-value" className="font-mono text-xs text-[#F0F4F8]">
          5m
        </span>
      </div>
      <div className="flex items-center gap-2">
        <span className="font-sans text-[10px] uppercase tracking-[0.15em] text-[#4B5563]">Data</span>
        <button data-testid="chart-origin-live-button" className={btn(origin === "LIVE")} onClick={() => onOrigin("LIVE")}>
          live sim
        </button>
        <button
          data-testid="chart-origin-replay-button"
          className={btn(origin === "REPLAY-SYNTHETIC")}
          onClick={() => onOrigin("REPLAY-SYNTHETIC")}
        >
          replay-synthetic
        </button>
      </div>
      <div className="flex items-center gap-2">
        <span className="font-sans text-[10px] uppercase tracking-[0.15em] text-[#4B5563]">Markers</span>
        <span data-testid="chart-markers-policy" className="font-mono text-[10px] text-[#8A99A8]">
          CE / PE / INVALIDATION
        </span>
        <button
          data-testid="chart-show-wait-toggle"
          aria-pressed={includeWait}
          className={btn(includeWait)}
          onClick={() => onIncludeWait(!includeWait)}
        >
          show wait: {includeWait ? "on" : "off"}
        </button>
      </div>
    </div>
  );
}
