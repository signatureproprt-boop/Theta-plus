import { Panel, Row, fmtNum, fmtSigned } from "./shared";
import type { PcrPanel as Pcr } from "@/lib/dashboardTypes";

export default function PcrAnalysisCard({ pcr }: { pcr?: Pcr }) {
  const trend = pcr?.pcr_trend ?? "INSUFFICIENT_DATA";
  const trendTone =
    trend === "UP" ? "text-[#34D399]" : trend === "DOWN" ? "text-[#F87171]" : "text-[#8A99A8]";
  return (
    <Panel title="PCR" testid="pcr-analysis-card">
      <div className="space-y-0.5">
        <Row label="Total PCR" value={fmtNum(pcr?.total_pcr, 4)} testid="pcr-total-value" />
        <Row label="ATM PCR" value={fmtNum(pcr?.atm_pcr, 4)} testid="pcr-atm-value" />
        <Row
          label="PCR Change"
          value={fmtSigned(pcr?.pcr_change, 4)}
          testid="pcr-change-value"
          tone={
            pcr?.pcr_change == null
              ? undefined
              : pcr.pcr_change > 0
                ? "text-[#34D399]"
                : pcr.pcr_change < 0
                  ? "text-[#F87171]"
                  : undefined
          }
        />
        <Row label="PCR Trend" value={trend} testid="pcr-trend-value" tone={trendTone} />
        <Row
          label="ATM PCR Trend"
          value={pcr?.atm_pcr_trend ?? "INSUFFICIENT_DATA"}
          testid="pcr-atm-trend-value"
          tone={
            pcr?.atm_pcr_trend === "UP"
              ? "text-[#34D399]"
              : pcr?.atm_pcr_trend === "DOWN"
                ? "text-[#F87171]"
                : "text-[#8A99A8]"
          }
        />
      </div>
    </Panel>
  );
}
