// Shared control-room helpers: status colors, pulsing dots, metric tiles, panels.
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export const STATUS = {
  OK: { text: "text-emerald-400", dot: "bg-emerald-500", label: "OK" },
  WARNING: { text: "text-amber-400", dot: "bg-amber-500", label: "WARNING" },
  STALE: { text: "text-red-400", dot: "bg-red-500", label: "STALE" },
  ERROR: { text: "text-rose-400", dot: "bg-rose-500", label: "ERROR" },
  DISABLED: { text: "text-slate-500", dot: "bg-slate-600", label: "DISABLED" },
} as const;

export type StatusKey = keyof typeof STATUS;

export function statusOf(key: string | undefined) {
  return STATUS[(key ?? "").toUpperCase() as StatusKey] ?? STATUS.DISABLED;
}

export function StatusDot({ status, testid }: { status: string; testid?: string }) {
  const s = statusOf(status);
  const pulsing = status === "STALE" || status === "ERROR";
  return (
    <span className="relative inline-flex h-2 w-2" data-testid={testid}>
      {pulsing && (
        <span className={cn("absolute inline-flex h-full w-full animate-ping rounded-full opacity-60", s.dot)} />
      )}
      <span className={cn("relative inline-flex h-2 w-2 rounded-full", s.dot)} />
    </span>
  );
}

export function Panel({
  title,
  testid,
  children,
  className,
  action,
}: {
  title: string;
  testid: string;
  children: ReactNode;
  className?: string;
  action?: ReactNode;
}) {
  return (
    <section
      data-testid={testid}
      className={cn("rounded-lg border border-[#1F2937] bg-[#11161D] flex flex-col", className)}
    >
      <header className="flex items-center justify-between border-b border-[#1F2937] px-4 py-2.5">
        <h2 className="font-mono text-[11px] uppercase tracking-[0.15em] text-[#8A99A8]">{title}</h2>
        {action}
      </header>
      <div className="flex-1 p-4">{children}</div>
    </section>
  );
}

export function MetricTile({
  label,
  value,
  testid,
  tone,
  sub,
}: {
  label: string;
  value: string;
  testid: string;
  tone?: string;
  sub?: string;
}) {
  return (
    <div className="rounded-lg border border-[#1F2937] bg-[#11161D] px-4 py-3">
      <div className="font-sans text-[10px] uppercase tracking-[0.15em] text-[#8A99A8]">{label}</div>
      <div
        data-testid={testid}
        className={cn("font-mono text-xl tracking-tight text-[#F0F4F8] mt-1", tone)}
      >
        {value}
      </div>
      {sub && <div className="font-mono text-[10px] text-[#8A99A8] mt-0.5">{sub}</div>}
    </div>
  );
}

export function Row({
  label,
  value,
  testid,
  tone,
}: {
  label: string;
  value: string;
  testid: string;
  tone?: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1">
      <span className="font-sans text-xs uppercase tracking-[0.1em] text-[#8A99A8]">{label}</span>
      <span data-testid={testid} className={cn("font-mono text-sm text-[#F0F4F8]", tone)}>
        {value}
      </span>
    </div>
  );
}

export function SkeletonRows({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-5 rounded border border-dashed border-[#1F2937] bg-[#0C1017]" />
      ))}
    </div>
  );
}

export function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function fmtSigned(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const s = v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return v >= 0 ? `+${s}` : s;
}

export function fmtInt(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toLocaleString("en-US");
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString("en-IN", { hour12: false, timeZone: "Asia/Kolkata" });
}
