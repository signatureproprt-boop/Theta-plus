import { cn } from "@/lib/utils";
import { fmtIst, num, type ChartSignalMarker } from "@/lib/chartTypes";
import { DECISION_TONE } from "./SignalDetailPanel";

// Phase F — backend signal overlay: the marker list drawn alongside the chart.
// Markers come from GET /api/chart/signals; the browser never derives a signal.
export default function SignalMarkerList({
  markers,
  selectedId,
  onSelect,
  isError,
}: {
  markers: ChartSignalMarker[];
  selectedId?: string;
  onSelect: (m: ChartSignalMarker) => void;
  isError?: boolean;
}) {
  return (
    <div data-testid="signal-marker-list" className="rounded-lg border border-[#1F2937] bg-[#11161D]">
      <header className="flex items-center justify-between border-b border-[#1F2937] px-4 py-2.5">
        <h2 className="font-mono text-[11px] uppercase tracking-[0.15em] text-[#8A99A8]">
          Backend Signal Overlay
        </h2>
        <span data-testid="signal-marker-count" className="font-mono text-[10px] text-[#4B5563]">
          {markers.length} markers
        </span>
      </header>
      <div className="max-h-[300px] overflow-y-auto p-2">
        {isError ? (
          <p
            data-testid="signal-data-unavailable"
            className="p-4 text-center font-mono text-[11px] uppercase tracking-[0.15em] text-amber-400"
          >
            Signal data unavailable
          </p>
        ) : markers.length === 0 ? (
          <p
            data-testid="signal-marker-empty"
            className="p-4 text-center font-mono text-[11px] uppercase tracking-[0.15em] text-[#4B5563]"
          >
            no CE / PE / invalidation markers yet
          </p>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {markers
              .slice()
              .reverse()
              .map((m) => (
                <li key={m.signal_id}>
                  <button
                    data-testid={`signal-marker-${m.signal_id}`}
                    onClick={() => onSelect(m)}
                    className={cn(
                      "flex w-full flex-wrap items-center gap-x-3 gap-y-1 rounded border px-3 py-2 text-left transition-colors duration-150",
                      selectedId === m.signal_id
                        ? "border-[#38BDF8] bg-[#0B1F2C]"
                        : "border-[#1A212C] bg-[#0C1017] hover:border-[#334155]",
                    )}
                  >
                    <span className="font-mono text-[11px] text-[#8A99A8]">{fmtIst(m.timestamp)}</span>
                    <span
                      className={cn(
                        "rounded border px-1.5 py-0.5 font-mono text-[10px] tracking-[0.08em]",
                        DECISION_TONE[m.decision] ?? DECISION_TONE.WAIT,
                      )}
                    >
                      {m.decision.replace("_", " ")}
                    </span>
                    <span className="font-mono text-[10px] text-[#CBD5E1]">
                      {m.score}/{m.max_score}
                    </span>
                    <span className="font-mono text-[10px] text-[#8A99A8]">PCR {num(m.pcr)}</span>
                    <span className="ml-auto font-mono text-[10px] text-[#4B5563]">{m.state}</span>
                  </button>
                </li>
              ))}
          </ul>
        )}
      </div>
    </div>
  );
}
