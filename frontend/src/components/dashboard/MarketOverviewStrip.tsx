import { MetricTile, fmtNum, fmtSigned, fmtTime } from "./shared";
import type { MarketPanel as Market } from "@/lib/dashboardTypes";

export default function MarketOverviewStrip({ market }: { market?: Market }) {
  const distance = market?.vwap_distance ?? null;
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
      <MetricTile label="Index" value={market?.index ?? "—"} testid="market-index-value" />
      <MetricTile label="Spot" value={fmtNum(market?.spot)} testid="market-spot-value" />
      <MetricTile label="VWAP" value={fmtNum(market?.vwap)} testid="market-vwap-value" />
      <MetricTile
        label="VWAP Distance"
        value={market ? fmtSigned(distance) : "—"}
        testid="market-vwap-distance-value"
        tone={market ? (market.above_vwap ? "text-[#34D399]" : market.below_vwap ? "text-[#F87171]" : "") : undefined}
        sub={market?.vwap_distance_percent != null ? `${fmtSigned(market.vwap_distance_percent)}%` : undefined}
      />
      <MetricTile label="ATM" value={market?.atm != null ? String(market.atm) : "—"} testid="market-atm-value" />
      <MetricTile label="Last Update" value={fmtTime(market?.last_update)} testid="market-last-update-value" sub="IST" />
    </div>
  );
}
