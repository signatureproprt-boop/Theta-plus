import { Check, Minus } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Panel, StatusDot, fmtTime } from "./shared";
import { cn } from "@/lib/utils";
import type { SignalPanel as Signal } from "@/lib/dashboardTypes";

// Score bar ratio out of the configured max (X/100 — never a percentage).
function ScoreBar({
  label,
  score,
  max,
  testid,
  barTestid,
  tone,
}: {
  label: string;
  score: number;
  max: number;
  testid: string;
  barTestid: string;
  tone: string;
}) {
  const pct = Math.max(0, Math.min(100, (score / (max || 100)) * 100));
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between">
        <span className="font-sans text-[10px] uppercase tracking-[0.15em] text-[#8A99A8]">{label}</span>
        <span data-testid={testid} className="font-mono text-sm text-[#F0F4F8]">
          {score}/{max}
        </span>
      </div>
      <div className="h-1.5 rounded bg-[#0C1017] border border-[#1F2937] overflow-hidden">
        <div data-testid={barTestid} className={cn("h-full rounded", tone)} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

const TONES = {
  CE_SETUP: { bg: "bg-[#052E1B]", border: "border-[#059669]", text: "text-[#34D399]", bar: "bg-[#10B981]" },
  PE_SETUP: { bg: "bg-[#360C14]", border: "border-[#DC2626]", text: "text-[#F87171]", bar: "bg-[#EF4444]" },
  WAIT: { bg: "bg-[#151B24]", border: "border-[#334155]", text: "text-[#E2E8F0]", bar: "bg-[#64748B]" },
} as const;

export default function SignalHeroCard({ signal }: { signal?: Signal }) {
  const code = signal?.decision_code ?? "WAIT";
  const tones = TONES[code as keyof typeof TONES] ?? TONES.WAIT;
  const label = signal?.decision ?? "WAIT";

  return (
    <Panel title="Final Signal" testid="signal-hero-card">
      <div className="flex flex-col gap-4">
        <div
          data-testid="signal-status-pill"
          className={cn(
            "rounded-lg border px-5 py-6 text-center",
            tones.bg,
            tones.border,
          )}
        >
          <div
            data-testid="signal-decision-label"
            className={cn("font-mono text-4xl font-semibold tracking-tight", tones.text, "drop-shadow-[0_0_12px_rgba(16,185,129,0.35)]")}
          >
            {label}
          </div>
          <div className="mt-2 flex items-center justify-center gap-3">
            <span
              data-testid="signal-state-pill"
              className="rounded border border-[#1F2937] bg-[#0C1017] px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.15em] text-[#8A99A8]"
            >
              state: {signal?.state ?? "—"}
            </span>
            <span className="font-mono text-[10px] text-[#8A99A8]">
              signal time {fmtTime(signal?.signal_time)}
            </span>
            <span className="font-mono text-[10px] text-[#8A99A8]">
              cooldown until {fmtTime(signal?.cooldown_until)}
            </span>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <ScoreBar
            label="CE Score"
            score={signal?.ce_score ?? 0}
            max={signal?.ce_max_score ?? 100}
            testid="ce-score-display"
            barTestid="ce-score-bar"
            tone={tones.bar}
          />
          <ScoreBar
            label="PE Score"
            score={signal?.pe_score ?? 0}
            max={signal?.pe_max_score ?? 100}
            testid="pe-score-display"
            barTestid="pe-score-bar"
            tone="bg-[#64748B]"
          />
        </div>

        <div className="grid grid-cols-1 gap-3">
          {signal && signal.confirmed.length > 0 && (
            <div>
              <div className="font-sans text-[10px] uppercase tracking-[0.15em] text-[#8A99A8] mb-1">Confirmed</div>
              <ul data-testid="signal-confirmed-list" className="space-y-1">
                {signal.confirmed.map((c) => (
                  <li key={c} className="flex items-center gap-2 font-mono text-xs text-[#34D399]">
                    <Check className="h-3 w-3" /> {c}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {signal && signal.unavailable.length > 0 && (
            <div>
              <div className="font-sans text-[10px] uppercase tracking-[0.15em] text-[#8A99A8] mb-1">Unavailable</div>
              <ul data-testid="signal-unavailable-list" className="space-y-1">
                {signal.unavailable.map((u) => (
                  <li key={u} className="flex items-center gap-2 font-mono text-xs text-[#8A99A8]">
                    <Minus className="h-3 w-3" /> {u}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {signal && signal.reasons.length > 0 && (
            <div>
              <div className="font-sans text-[10px] uppercase tracking-[0.15em] text-[#8A99A8] mb-1">Reason</div>
              <ul data-testid="signal-reasons-list" className="space-y-1">
                {signal.reasons.slice(0, 4).map((r, i) => (
                  <li key={i} className="font-mono text-[11px] leading-relaxed text-[#E2E8F0]">
                    {r}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>

        <div className="flex items-center gap-2 border-t border-[#1F2937] pt-3">
          <Badge variant="outline" className="border-[#1F2937] font-mono text-[9px] text-[#8A99A8]">
            SIGNAL-ONLY · NO ORDER EXECUTION
          </Badge>
          <StatusDot status="OK" testid="signal-safety-dot" />
        </div>
      </div>
    </Panel>
  );
}
