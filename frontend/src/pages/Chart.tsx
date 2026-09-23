import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/lib/api";
import {
  fmtIst,
  num,
  type ChartSignalMarker,
  type ChartSignalsResponse,
  type DataOrigin,
} from "@/lib/chartTypes";
import TradingViewWidget from "@/components/chart/TradingViewWidget";
import ChartControls from "@/components/chart/ChartControls";
import SignalMarkerList from "@/components/chart/SignalMarkerList";
import SignalDetailPanel from "@/components/chart/SignalDetailPanel";
import PaperLevelsPanel from "@/components/chart/PaperLevelsPanel";

// Phase F — NIFTY 5m chart research screen.
// TradingView = visualization only. Backend = strategy authority for VWAP,
// PCR, OI, score, decision, state and invalidation. Nothing is computed here.
export default function Chart() {
  const [origin, setOrigin] = useState<DataOrigin>("LIVE");
  const [includeWait, setIncludeWait] = useState(false);
  const [selected, setSelected] = useState<ChartSignalMarker | undefined>();

  const { data, isError } = useQuery({
    queryKey: ["chart-signals", origin, includeWait],
    queryFn: () =>
      apiGet<ChartSignalsResponse>(
        `/chart/signals?origin=${encodeURIComponent(origin)}&include_wait=${includeWait}`,
      ),
    refetchInterval: origin === "LIVE" ? 5000 : false,
  });

  const markers = data?.markers ?? [];

  // Keep the detail panel in sync with the selected origin/marker set.
  useEffect(() => {
    setSelected(undefined);
  }, [origin]);
  useEffect(() => {
    if (!selected && markers.length > 0) setSelected(markers[markers.length - 1]);
  }, [markers, selected]);

  return (
    <div className="min-h-screen overflow-x-hidden bg-[#090B0E] text-[#F0F4F8]">
      <div className="mx-auto w-full max-w-[1720px] space-y-4 px-3 py-4 sm:px-4">
        <header
          data-testid="chart-header"
          className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-[#1F2937] bg-[#11161D] px-4 py-3"
        >
          <h1 data-testid="chart-title" className="font-mono text-sm uppercase tracking-[0.2em] text-[#F0F4F8]">
            NIFTY 5m
          </h1>
          <span
            data-testid="chart-origin-label"
            className="rounded border border-[#1F2937] px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.15em] text-[#7DD3FC]"
          >
            {data?.data_origin_label ?? (origin === "LIVE" ? "LIVE • SIMULATED DATA" : "REPLAY • SYNTHETIC DATA")}
          </span>
          {data?.stale && (
            <span
              data-testid="chart-stale-badge"
              className="rounded border border-red-500 px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.15em] text-red-400"
            >
              STALE DATA
            </span>
          )}
          <Link
            data-testid="chart-back-to-control-room-link"
            to="/"
            className="ml-auto rounded border border-[#1F2937] px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.15em] text-[#8A99A8] transition-colors duration-150 hover:text-[#E2E8F0]"
          >
            ← control room
          </Link>
        </header>

        <ChartControls
          origin={origin}
          onOrigin={setOrigin}
          includeWait={includeWait}
          onIncludeWait={setIncludeWait}
        />

        <TradingViewWidget symbol={data?.tradingview_symbol ?? "NSE:NIFTY"} interval="5" />

        <p data-testid="chart-vwap-authority-note" className="font-sans text-[10px] leading-relaxed text-[#4B5563]">
          TradingView VWAP = visual reference only. Backend VWAP (shown below) is the
          strategy-authoritative value. Scores are shown as X/100 — never a probability.
        </p>
        <p data-testid="chart-widget-limitation-note" className="font-sans text-[10px] leading-relaxed text-[#4B5563]">
          Note: the free TradingView widget may report “symbol only available on TradingView”
          for NSE:NIFTY index data depending on region/data permissions. That is a widget-side
          data entitlement, not a backend failure — all signal data below stays fully available.
        </p>

        <section
          data-testid="latest-signal-strip"
          className="grid grid-cols-2 gap-3 rounded-lg border border-[#1F2937] bg-[#11161D] p-4 sm:grid-cols-3 lg:grid-cols-6"
        >
          {[
            { label: "Latest", value: (data?.latest_decision ?? "WAIT").replace("_", " "), testid: "latest-decision" },
            { label: "State", value: data?.latest_state ?? "WAIT", testid: "latest-state" },
            { label: "Spot", value: num(data?.spot ?? null), testid: "latest-spot" },
            { label: "Backend VWAP", value: num(data?.vwap ?? null), testid: "latest-vwap" },
            { label: "Distance", value: num(data?.vwap_distance ?? null), testid: "latest-vwap-distance" },
            { label: "Data Health", value: data?.data_health ?? "—", testid: "latest-data-health" },
          ].map((tile) => (
            <div key={tile.testid} className="min-w-0">
              <span className="block font-sans text-[10px] uppercase tracking-[0.15em] text-[#4B5563]">
                {tile.label}
              </span>
              <span data-testid={tile.testid} className="mt-1 block truncate font-mono text-sm text-[#F0F4F8]">
                {tile.value}
              </span>
            </div>
          ))}
        </section>

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
          <div className="lg:col-span-5">
            <SignalMarkerList
              markers={markers}
              selectedId={selected?.signal_id}
              onSelect={setSelected}
              isError={isError}
            />
          </div>
          <div className="lg:col-span-7 space-y-4">
            <SignalDetailPanel marker={selected} />
            <PaperLevelsPanel signalId={selected?.signal_id} />
          </div>
        </div>

        <footer className="space-y-1 pb-4">
          {(data?.notes ?? []).map((note) => (
            <p key={note} data-testid="chart-note" className="font-sans text-[10px] leading-relaxed text-[#4B5563]">
              {note}
            </p>
          ))}
          <p data-testid="chart-generated-at" className="font-mono text-[10px] text-[#33404F]">
            payload generated {fmtIst(data?.generated_at)} IST · signal-only research view · no order execution exists
          </p>
        </footer>
      </div>
    </div>
  );
}
