import { Panel, Row, fmtInt, fmtSigned } from "./shared";
import type { OiPanel as Oi } from "@/lib/dashboardTypes";

export default function OiAnalysisCard({ oi }: { oi?: Oi }) {
  const put = oi?.put_oi ?? 0;
  const call = oi?.call_oi ?? 0;
  const total = put + call;
  const putShare = total > 0 ? (put / total) * 100 : 50;
  return (
    <Panel title="OI" testid="oi-analysis-card">
      <div className="space-y-0.5">
        <Row label="Put OI" value={fmtInt(oi?.put_oi)} testid="oi-put-value" />
        <Row label="Call OI" value={fmtInt(oi?.call_oi)} testid="oi-call-value" />
        <Row
          label="Put OI Change"
          value={fmtSigned(oi?.put_oi_change, 0)}
          testid="oi-put-change-value"
          tone={(oi?.put_oi_change ?? 0) >= 0 ? "text-[#34D399]" : "text-[#F87171]"}
        />
        <Row
          label="Call OI Change"
          value={fmtSigned(oi?.call_oi_change, 0)}
          testid="oi-call-change-value"
          tone={(oi?.call_oi_change ?? 0) >= 0 ? "text-[#34D399]" : "text-[#F87171]"}
        />
      </div>
      <div className="mt-3" data-testid="oi-balance-bar">
        <div className="flex justify-between font-mono text-[9px] uppercase tracking-[0.15em] text-[#8A99A8] mb-1">
          <span>put {putShare.toFixed(0)}%</span>
          <span>call {(100 - putShare).toFixed(0)}%</span>
        </div>
        <div className="flex h-1.5 rounded overflow-hidden border border-[#1F2937]">
          <div className="bg-[#10B981]" style={{ width: `${putShare}%` }} />
          <div className="bg-[#64748B]" style={{ width: `${100 - putShare}%` }} />
        </div>
      </div>
    </Panel>
  );
}
