import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { apiGet } from "@/lib/api";
import type { DashboardPayload } from "@/lib/dashboardTypes";

import HeaderSystemBar from "@/components/dashboard/HeaderSystemBar";
import MarketOverviewStrip from "@/components/dashboard/MarketOverviewStrip";
import SignalHeroCard from "@/components/dashboard/SignalHeroCard";
import PcrAnalysisCard from "@/components/dashboard/PcrAnalysisCard";
import OiAnalysisCard from "@/components/dashboard/OiAnalysisCard";
import OptionLtpCard from "@/components/dashboard/OptionLtpCard";
import DataHealthCard from "@/components/dashboard/DataHealthCard";
import SettingsConfigCard from "@/components/dashboard/SettingsConfigCard";
import SystemLogCard from "@/components/dashboard/SystemLogCard";
import PaperTradingCard from "@/components/dashboard/PaperTradingCard";
import ExecutionStatusCard from "@/components/dashboard/ExecutionStatusCard";
import ResearchAnalyticsCard from "@/components/dashboard/ResearchAnalyticsCard";

// NIFTY PCR + OI + VWAP control room. The shell renders unconditionally; only
// data regions show skeletons when the backend is unreachable (static preview).
export default function Home() {
  const { data, isError, isLoading } = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => apiGet<DashboardPayload>("/dashboard"),
    refetchInterval: 5000,
  });

  return (
    <div className="min-h-screen bg-[#090B0E] text-[#F0F4F8]">
      <div className="w-full max-w-[1720px] mx-auto px-4 py-4 space-y-4">
        <div className="flex justify-end">
          <Link
            data-testid="nav-chart-link"
            to="/chart"
            className="rounded border border-[#1F2937] bg-[#11161D] px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.15em] text-[#8A99A8] transition-colors duration-150 hover:text-[#7DD3FC]"
          >
            nifty chart + signal markers →
          </Link>
        </div>
        <HeaderSystemBar data={data} isError={isError} />
        <MarketOverviewStrip market={data?.market} />
        <PaperTradingCard />
        <ExecutionStatusCard />
        <ResearchAnalyticsCard />

        <div className="grid grid-cols-1 lg:grid-cols-12 gap-4">
          <div className="lg:col-span-5 flex flex-col gap-4">
            <SignalHeroCard signal={data?.signal} />
            <OptionLtpCard option={data?.option} />
          </div>
          <div className="lg:col-span-7 grid grid-cols-1 md:grid-cols-2 gap-4 content-start">
            <PcrAnalysisCard pcr={data?.pcr} />
            <OiAnalysisCard oi={data?.oi} />
            <div className="md:col-span-2">
              <SystemLogCard entries={data?.system_log} />
            </div>
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-12 gap-4">
          <div className="lg:col-span-5">
            <DataHealthCard health={data?.data_health} />
          </div>
          <div className="lg:col-span-7">
            <SettingsConfigCard settings={data?.settings} />
          </div>
        </div>

        {isLoading && !data && (
          <div className="text-center font-mono text-[10px] uppercase tracking-[0.15em] text-[#8A99A8]">
            connecting to signal engine…
          </div>
        )}
      </div>
    </div>
  );
}
