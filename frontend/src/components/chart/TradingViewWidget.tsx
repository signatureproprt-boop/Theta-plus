import { useEffect, useRef, useState } from "react";

// Phase F — TradingView Advanced Chart widget embed. VISUALIZATION ONLY:
// no API key, no strategy authority, no alerts/orders. The widget's built-in
// VWAP is a VISUAL REFERENCE; the strategy VWAP comes from the backend.
// If the script fails or is blocked, we degrade gracefully — backend signal
// data stays fully usable.
export default function TradingViewWidget({
  symbol = "NSE:NIFTY",
  interval = "5",
}: {
  symbol?: string;
  interval?: string;
}) {
  const holder = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "unavailable">("loading");

  useEffect(() => {
    const el = holder.current;
    if (!el) return;
    el.innerHTML = "";

    const script = document.createElement("script");
    script.src = "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";
    script.type = "text/javascript";
    script.async = true;
    script.innerHTML = JSON.stringify({
      symbol,
      interval,
      timezone: "Asia/Kolkata",
      theme: "dark",
      style: "1",
      locale: "in",
      hide_side_toolbar: true,
      allow_symbol_change: false,
      save_image: false,
      studies: ["STD;VWAP"], // visual reference only
      support_host: "https://www.tradingview.com",
      autosize: true,
    });
    script.onerror = () => setStatus("unavailable");
    el.appendChild(script);

    // The embed injects an iframe; if none appears the widget is blocked.
    const timer = window.setTimeout(() => {
      setStatus(el.querySelector("iframe") ? "ready" : "unavailable");
    }, 6000);

    return () => {
      window.clearTimeout(timer);
      el.innerHTML = "";
    };
  }, [symbol, interval]);

  return (
    <div
      data-testid="tradingview-chart-container"
      data-widget-status={status}
      className="relative h-[320px] sm:h-[420px] lg:h-[520px] w-full overflow-hidden rounded-lg border border-[#1F2937] bg-[#0C1017]"
    >
      <div ref={holder} className="tradingview-widget-container h-full w-full" />
      {status === "unavailable" && (
        <div
          data-testid="tradingview-unavailable-notice"
          className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-[#0C1017] px-6 text-center"
        >
          <span className="font-mono text-xs uppercase tracking-[0.15em] text-amber-400">
            TradingView unavailable
          </span>
          <span className="font-sans text-[11px] text-[#8A99A8]">
            Backend signal data remains available below.
          </span>
        </div>
      )}
      {status === "loading" && (
        <div
          data-testid="tradingview-loading-notice"
          className="pointer-events-none absolute bottom-2 left-3 font-mono text-[10px] uppercase tracking-[0.15em] text-[#4B5563]"
        >
          loading chart…
        </div>
      )}
    </div>
  );
}
