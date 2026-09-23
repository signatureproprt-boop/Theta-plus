import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/lib/api";
import { Panel } from "./shared";
import type { DatasetStatus, ReadinessReport, ValidationReport } from "@/lib/researchTypes";

// Phase I — research analytics + production readiness. Read-only, and every
// figure carries its data origin, date range, sample size and strategy version.

const tone = (status: string) =>
  status === "PASS" ? "text-[#8AE6A2]" : status === "FAIL" ? "text-[#F87171]" : "text-[#E9C46A]";

function Stat({ label, value, testid }: { label: string; value: string; testid: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2 font-mono text-[11px]">
      <span className="text-[#4B5563]">{label}</span>
      <span data-testid={testid} className="text-[#D6DEE7]">{value}</span>
    </div>
  );
}

export default function ResearchAnalyticsCard() {
  const { data: status, isError } = useQuery({
    queryKey: ["research-status"],
    queryFn: () => apiGet<DatasetStatus>("/research/status"),
    refetchInterval: 30000,
  });
  const { data: readiness } = useQuery({
    queryKey: ["research-readiness"],
    queryFn: () => apiGet<ReadinessReport>("/research/readiness"),
    refetchInterval: 30000,
  });
  const { data: report } = useQuery({
    queryKey: ["research-report"],
    queryFn: () => apiGet<ValidationReport>("/research/report"),
    retry: false,
    refetchInterval: 30000,
  });

  const { data: instrumentMap } = useQuery({
    queryKey: ["research-instrument-map"],
    queryFn: () => apiGet<{ status: string; rows: number; broker_verified: boolean }>(
      "/research/instrument-map",
    ),
    refetchInterval: 30000,
  });

  const analytics = report?.analytics;

  return (
    <Panel
      title="research analytics / production readiness"
      action={
        <span
          data-testid="research-origin-badge"
          className="rounded border border-[#1F2937] bg-[#11161D] px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.18em] text-[#8A99A8]"
        >
          {report?.data_origin ?? (status?.real_historical_status === "PASS"
            ? "REAL_HISTORICAL"
            : "NO REAL DATASET")}
        </span>
      }
      testid="research-analytics-card"
    >
      {isError && (
        <p data-testid="research-error" className="font-mono text-[11px] text-[#F87171]">
          research status unavailable
        </p>
      )}

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        <div className="space-y-1 rounded border border-[#1F2937] bg-[#0D1117] p-3">
          <p className="font-mono text-[9px] uppercase tracking-[0.18em] text-[#4B5563]">data sources</p>
          <Stat
            label="real historical"
            value={status?.real_historical_status ?? "—"}
            testid="research-historical-status"
          />
          <Stat
            label="live real (dhan)"
            value={status?.live_real_data_status ?? "—"}
            testid="research-live-real-status"
          />
          <Stat
            label="instrument map"
            value={instrumentMap?.status ?? "PENDING"}
            testid="research-instrument-map"
          />
          <Stat
            label="broker verified"
            value={instrumentMap?.broker_verified ? "YES" : "PENDING"}
            testid="research-broker-verified"
          />
          <Stat
            label="dataset id"
            value={report?.dataset.dataset_id ?? "—"}
            testid="research-dataset-id"
          />
          <Stat
            label="dataset hash"
            value={report ? report.dataset.dataset_hash.slice(0, 12) : "—"}
            testid="research-dataset-hash"
          />
          <Stat
            label="date range"
            value={report ? `${report.dataset.date_from}..${report.dataset.date_to}` : "—"}
            testid="research-date-range"
          />
        </div>

        <div className="space-y-1 rounded border border-[#1F2937] bg-[#0D1117] p-3">
          <p className="font-mono text-[9px] uppercase tracking-[0.18em] text-[#4B5563]">
            measured outcomes
          </p>
          <Stat
            label="signals (N)"
            value={analytics ? String(analytics.sample_size_signals) : "—"}
            testid="research-sample-signals"
          />
          <Stat
            label="resolved (N)"
            value={analytics ? String(analytics.sample_size_resolved) : "—"}
            testid="research-sample-resolved"
          />
          <Stat
            label="gross points"
            value={analytics?.gross_points_total != null ? String(analytics.gross_points_total) : "—"}
            testid="research-gross-points"
          />
          <Stat
            label="net points"
            value={analytics?.net_points_total != null ? String(analytics.net_points_total) : "—"}
            testid="research-net-points"
          />
          <Stat
            label="max drawdown"
            value={analytics?.max_drawdown_points != null ? String(analytics.max_drawdown_points) : "—"}
            testid="research-drawdown"
          />
          <Stat
            label="strategy / features"
            value={report ? `${report.dataset.strategy_version} / ${report.dataset.feature_version}` : "—"}
            testid="research-versions"
          />
        </div>

        <div className="space-y-1 rounded border border-[#1F2937] bg-[#0D1117] p-3">
          <p className="font-mono text-[9px] uppercase tracking-[0.18em] text-[#4B5563]">audits</p>
          <Stat
            label="data quality"
            value={report ? (report.quality.production_quality ? "PASS" : "FAIL") : "PENDING"}
            testid="research-quality-status"
          />
          <Stat
            label="future leakage"
            value={report ? (report.leakage.passed ? "PASS" : "FAIL") : "PENDING"}
            testid="research-leakage-status"
          />
          <Stat
            label="session reset"
            value={report ? (report.session_reset.passed ? "PASS" : "FAIL") : "PENDING"}
            testid="research-session-status"
          />
          <Stat
            label="determinism"
            value={report ? (report.determinism.passed ? "PASS" : "FAIL") : "PENDING"}
            testid="research-determinism-status"
          />
          <Stat
            label="readiness pass/pending"
            value={readiness ? `${readiness.passed}/${readiness.pending}` : "—"}
            testid="research-readiness-counts"
          />
          <Stat
            label="mandatory gates pending"
            value={readiness ? String(readiness.mandatory_pending.length) : "—"}
            testid="research-mandatory-pending"
          />
          <Stat
            label="telegram"
            value={readiness?.telegram_required ? "REQUIRED" : "OPTIONAL"}
            testid="research-telegram-optional"
          />
          <Stat
            label="live execution"
            value={readiness?.live_execution_enabled ? "ENABLED" : "OFF"}
            testid="research-live-flag"
          />
        </div>
      </div>

      <details className="mt-3">
        <summary
          data-testid="research-checklist-summary"
          className="cursor-pointer font-mono text-[10px] uppercase tracking-[0.18em] text-[#8A99A8]"
        >
          production readiness checklist
        </summary>
        <ul data-testid="research-checklist" className="mt-2 space-y-0.5 font-mono text-[10px]">
          {(readiness?.items ?? []).map((item) => (
            <li key={item.item} className="flex items-baseline justify-between gap-3">
              <span className="text-[#8A99A8]">{item.item}</span>
              <span className={tone(item.status)}>{item.status}</span>
            </li>
          ))}
        </ul>
      </details>

      <p
        data-testid="research-note"
        className="mt-3 rounded border border-[#1F2937] bg-[#0D1117] px-3 py-2 font-sans text-[10px] leading-relaxed text-[#4B5563]"
      >
        {report
          ? report.notes.join(" ")
          : (status?.note ?? "REAL HISTORICAL VALIDATION = PENDING — no genuine dataset configured.")}
      </p>
    </Panel>
  );
}
