import { cn } from "@/lib/utils";
import { fmtIst, fmtIstDate, int, num, type ChartSignalMarker } from "@/lib/chartTypes";

export const DECISION_TONE: Record<string, string> = {
  CE_SETUP: "border-[#059669] bg-[#052E1B] text-[#34D399]",
  PE_SETUP: "border-[#DC2626] bg-[#360C14] text-[#F87171]",
  CE_INVALIDATED: "border-[#B45309] bg-[#2A1B05] text-[#FBBF24]",
  PE_INVALIDATED: "border-[#B45309] bg-[#2A1B05] text-[#FBBF24]",
  WAIT: "border-[#334155] bg-[#151B24] text-[#E2E8F0]",
};

function Row({ label, value, testid }: { label: string; value: string; testid: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-[#161C25] py-1.5 last:border-b-0">
      <span className="font-sans text-[10px] uppercase tracking-[0.15em] text-[#8A99A8]">{label}</span>
      <span data-testid={testid} className="font-mono text-xs text-[#F0F4F8] text-right break-all">
        {value}
      </span>
    </div>
  );
}

// Phase F — marker detail. Every value is rendered from the backend marker;
// nothing here is recalculated in the browser.
export default function SignalDetailPanel({ marker }: { marker?: ChartSignalMarker }) {
  if (!marker) {
    return (
      <div
        data-testid="signal-detail-empty"
        className="rounded-lg border border-[#1F2937] bg-[#11161D] p-6 text-center font-mono text-[11px] uppercase tracking-[0.15em] text-[#4B5563]"
      >
        select a signal marker
      </div>
    );
  }

  const isInvalidation = marker.marker_kind === "INVALIDATION";
  return (
    <div data-testid="signal-detail-panel" className="rounded-lg border border-[#1F2937] bg-[#11161D]">
      <header className="flex flex-wrap items-center gap-3 border-b border-[#1F2937] px-4 py-3">
        <span
          data-testid="signal-detail-decision"
          className={cn("rounded border px-2.5 py-1 font-mono text-[11px] tracking-[0.1em]", DECISION_TONE[marker.decision] ?? DECISION_TONE.WAIT)}
        >
          {marker.decision.replace("_", " ")}
        </span>
        <span data-testid="signal-detail-state" className="font-mono text-[11px] text-[#8A99A8]">
          STATE {marker.state}
        </span>
        <span
          data-testid="signal-detail-origin"
          className="ml-auto font-mono text-[10px] uppercase tracking-[0.15em] text-[#7DD3FC]"
        >
          {marker.data_origin_label}
        </span>
        {marker.stale && (
          <span
            data-testid="signal-detail-stale-badge"
            className="rounded border border-red-500 px-2 py-0.5 font-mono text-[10px] text-red-400"
          >
            STALE DATA
          </span>
        )}
      </header>

      <div className="grid grid-cols-1 gap-x-6 p-4 md:grid-cols-2">
        <div>
          <Row label="Signal ID" value={marker.signal_id} testid="signal-detail-id" />
          <Row
            label="Timestamp IST"
            value={`${fmtIstDate(marker.timestamp)} ${fmtIst(marker.timestamp)}`}
            testid="signal-detail-timestamp"
          />
          <Row label="Spot" value={num(marker.spot)} testid="signal-detail-spot" />
          <Row label="Backend VWAP" value={num(marker.vwap)} testid="signal-detail-vwap" />
          <Row label="VWAP Distance" value={num(marker.vwap_distance)} testid="signal-detail-vwap-distance" />
          <Row label="ATM" value={marker.atm === null ? "—" : String(marker.atm)} testid="signal-detail-atm" />
          <Row label="PCR" value={num(marker.pcr)} testid="signal-detail-pcr" />
          <Row label="ATM PCR" value={num(marker.atm_pcr)} testid="signal-detail-atm-pcr" />
          <Row label="PCR Trend" value={marker.pcr_trend ?? "—"} testid="signal-detail-pcr-trend" />
        </div>
        <div>
          <Row label="CE OI" value={int(marker.ce_oi)} testid="signal-detail-ce-oi" />
          <Row label="CE OI Change" value={int(marker.ce_oi_change)} testid="signal-detail-ce-oi-change" />
          <Row label="PE OI" value={int(marker.pe_oi)} testid="signal-detail-pe-oi" />
          <Row label="PE OI Change" value={int(marker.pe_oi_change)} testid="signal-detail-pe-oi-change" />
          <Row
            label="Score"
            value={`${marker.score}/${marker.max_score}`}
            testid="signal-detail-score"
          />
          <Row label="Data Health" value={marker.data_health} testid="signal-detail-data-health" />
          <Row label="Strategy Version" value={marker.strategy_version || "—"} testid="signal-detail-strategy-version" />
          <Row label="Feature Version" value={marker.feature_version || "—"} testid="signal-detail-feature-version" />
          <Row label="Data Origin" value={marker.data_origin} testid="signal-detail-data-origin" />
        </div>
      </div>

      <div className="border-t border-[#1F2937] px-4 py-3">
        <span className="font-sans text-[10px] uppercase tracking-[0.15em] text-[#8A99A8]">Reason</span>
        <p data-testid="signal-detail-reason" className="mt-1 font-mono text-[11px] leading-relaxed text-[#CBD5E1]">
          {marker.reason || "—"}
        </p>
      </div>

      {isInvalidation && (
        <div data-testid="signal-detail-invalidation-block" className="border-t border-[#1F2937] px-4 py-3">
          <Row
            label="Original Signal"
            value={marker.origin_signal_id ?? "—"}
            testid="signal-detail-origin-signal-id"
          />
          <Row
            label="Invalidation Timestamp"
            value={fmtIst(marker.invalidation_timestamp)}
            testid="signal-detail-invalidation-timestamp"
          />
          <Row
            label="Invalidation Reason"
            value={marker.invalidation_reason ?? "—"}
            testid="signal-detail-invalidation-reason"
          />
        </div>
      )}
    </div>
  );
}
