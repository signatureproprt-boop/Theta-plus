import { Panel, Row, fmtInt, fmtNum } from "./shared";
import type { OptionPanel as Option } from "@/lib/dashboardTypes";

export default function OptionLtpCard({ option }: { option?: Option }) {
  return (
    <Panel title="ATM Option" testid="option-ltp-card">
      <div className="grid grid-cols-2 gap-4">
        <div className="rounded border border-[#1F2937] bg-[#0C1017] p-3">
          <div className="font-sans text-[10px] uppercase tracking-[0.15em] text-[#8A99A8]">
            ATM {option?.atm_strike ?? "—"} CE
          </div>
          <div data-testid="option-atm-ce-ltp" className="font-mono text-2xl text-[#34D399] mt-1">
            {fmtNum(option?.atm_ce_ltp)}
          </div>
          <div className="mt-1 space-y-0.5 font-mono text-[10px] text-[#8A99A8]">
            <div data-testid="option-ce-oi">OI {fmtInt(option?.ce_oi)}</div>
            <div data-testid="option-ce-oi-change">chg {fmtInt(option?.ce_oi_change)}</div>
            <div data-testid="option-ce-iv">IV {fmtNum(option?.ce_iv)}</div>
          </div>
        </div>
        <div className="rounded border border-[#1F2937] bg-[#0C1017] p-3">
          <div className="font-sans text-[10px] uppercase tracking-[0.15em] text-[#8A99A8]">
            ATM {option?.atm_strike ?? "—"} PE
          </div>
          <div data-testid="option-atm-pe-ltp" className="font-mono text-2xl text-[#F87171] mt-1">
            {fmtNum(option?.atm_pe_ltp)}
          </div>
          <div className="mt-1 space-y-0.5 font-mono text-[10px] text-[#8A99A8]">
            <div data-testid="option-pe-oi">OI {fmtInt(option?.pe_oi)}</div>
            <div data-testid="option-pe-oi-change">chg {fmtInt(option?.pe_oi_change)}</div>
            <div data-testid="option-pe-iv">IV {fmtNum(option?.pe_iv)}</div>
          </div>
        </div>
      </div>
      <Row label="Research SL/Target" value={`${option ? "" : "—"}SL 30 / TGT 35 pts`} testid="option-sl-target" />
    </Panel>
  );
}
