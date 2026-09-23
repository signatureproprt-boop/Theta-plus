import { fmtIst, fmtIstDate, num, type ChartSignalMarker } from "@/lib/chartTypes";

type Point = { marker: ChartSignalMarker; x: number; spotY: number; vwapY: number | null };

// Plot only backend observations. These are sampled prices, not OHLC candles or fills.
export default function DecisionTrace({
  markers,
  selectedId,
  onSelect,
}: {
  markers: ChartSignalMarker[];
  selectedId?: string;
  onSelect: (marker: ChartSignalMarker) => void;
}) {
  const valid = markers.filter((m) => m.spot !== null && Number.isFinite(m.spot));
  const sessionDate = fmtIstDate(valid[valid.length - 1]?.timestamp);
  const recent = valid.filter((m) => fmtIstDate(m.timestamp) === sessionDate).slice(-120);
  const values = recent.flatMap((m) => [m.spot, m.vwap].filter((v): v is number => v !== null && Number.isFinite(v)));
  if (recent.length < 2 || values.length < 2) {
    return (
      <section data-testid="decision-trace-empty" className="rounded-lg border border-[#1F2937] bg-[#11161D] p-6 font-mono text-xs text-[#8A99A8]">
        Waiting for at least two backend price observations. No price path is invented.
      </section>
    );
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  const padding = Math.max((max - min) * 0.08, 1);
  const low = min - padding;
  const range = max - min + 2 * padding;
  const width = 880;
  const height = 250;
  const xFor = (index: number) => 48 + (index / (recent.length - 1)) * (width - 70);
  const yFor = (value: number) => 20 + ((max + padding - value) / range) * (height - 50);
  const points: Point[] = recent.map((marker, index) => ({
    marker,
    x: xFor(index),
    spotY: yFor(marker.spot!),
    vwapY: marker.vwap === null ? null : yFor(marker.vwap),
  }));
  const hasGap = (a: Point, b: Point) => new Date(b.marker.timestamp).getTime() - new Date(a.marker.timestamp).getTime() > 120_000;
  const spotPath = points.map((p, i) => `${i === 0 || hasGap(points[i - 1], p) ? "M" : "L"}${p.x},${p.spotY}`).join(" ");
  const vwapRuns: Point[][] = [];
  for (const [index, point] of points.entries()) {
    if (point.vwapY === null) continue;
    if (vwapRuns.length === 0 || index === 0 || points[index - 1].vwapY === null || hasGap(points[index - 1], point)) vwapRuns.push([]);
    vwapRuns[vwapRuns.length - 1].push(point);
  }

  return (
    <section data-testid="decision-trace" className="rounded-lg border border-[#1F2937] bg-[#11161D] p-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-mono text-xs uppercase tracking-[0.15em]">Decision trace</h2>
        <div className="flex gap-4 font-mono text-[10px] text-[#8A99A8]">
          <span className="text-[#7DD3FC]">━ Spot</span>
          <span className="text-[#FBBF24]">┄ Backend VWAP</span>
          <span>● CE / PE / invalidation</span>
        </div>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Backend spot and VWAP observations with clickable signal markers" className="w-full">
        {[0, 0.5, 1].map((fraction) => {
          const value = max + padding - fraction * range;
          const y = 20 + fraction * (height - 50);
          return <g key={fraction}>
            <line x1="48" y1={y} x2="858" y2={y} stroke="#243040" strokeDasharray="3 5" />
            <text x="42" y={y + 4} textAnchor="end" fill="#8A99A8" fontSize="10">{num(value, 0)}</text>
          </g>;
        })}
        <path d={spotPath} fill="none" stroke="#7DD3FC" strokeWidth="2" />
        {vwapRuns.map((run, i) => run.length > 1 && (
          <path key={i} d={run.map((p, j) => `${j === 0 ? "M" : "L"}${p.x},${p.vwapY}`).join(" ")} fill="none" stroke="#FBBF24" strokeDasharray="5 4" strokeWidth="1.5" />
        ))}
        {points.filter((p) => p.marker.marker_kind !== "WAIT").map((p) => {
          const color = p.marker.marker_kind === "INVALIDATION" ? "#FBBF24" : p.marker.side === "CE" ? "#34D399" : "#F87171";
          return <circle key={p.marker.signal_id} data-testid={`trace-${p.marker.signal_id}`} cx={p.x} cy={p.spotY} r={selectedId === p.marker.signal_id ? 8 : 5} fill={color} stroke="#090B0E" strokeWidth="2" role="button" tabIndex={0} aria-label={`${p.marker.decision} at ${fmtIst(p.marker.timestamp)}`} onClick={() => onSelect(p.marker)} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") onSelect(p.marker); }} />;
        })}
        <text x="48" y="246" fill="#8A99A8" fontSize="10">{fmtIst(recent[0].timestamp)} IST</text>
        <text x="858" y="246" textAnchor="end" fill="#8A99A8" fontSize="10">{fmtIst(recent[recent.length - 1].timestamp)} IST</text>
      </svg>
      <p className="mt-2 text-[10px] text-[#8A99A8]">{sessionDate}: last {recent.length} backend observations. Click a marker for its evidence. Points are equally spaced observations, not candles or elapsed-time scale. Spot movement is not option premium or profit; feed gaps break the line.</p>
    </section>
  );
}
