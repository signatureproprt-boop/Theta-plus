import { Panel, Row } from "./shared";
import type { SettingsPanel as Settings } from "@/lib/dashboardTypes";

export default function SettingsConfigCard({ settings }: { settings?: Settings }) {
  if (!settings) {
    return (
      <Panel title="Settings" testid="settings-config-card">
        <div className="font-mono text-xs text-[#8A99A8]">settings unavailable</div>
      </Panel>
    );
  }
  return (
    <Panel title="Settings" testid="settings-config-card">
      <div className="grid grid-cols-2 gap-x-6">
        <Row label="Index" value={settings.index} testid="settings-index" />
        <Row label="Timezone" value={settings.timezone} testid="settings-timezone" />
        <Row label="Market Open" value={settings.market_open} testid="settings-market-open" />
        <Row label="Signal Start" value={settings.signal_start} testid="settings-signal-start" />
        <Row label="Signal End" value={settings.signal_end} testid="settings-signal-end" />
        <Row label="ATM Range" value={`±${settings.atm_range}`} testid="settings-atm-range" />
        <Row label="PCR Lookback" value={String(settings.pcr_lookback)} testid="settings-pcr-lookback" />
        <Row label="Min Score" value={`${settings.min_score}/100`} testid="settings-min-score" />
        <Row label="Target" value={`${settings.target_points} pts`} testid="settings-target-points" />
        <Row label="Stoploss" value={`${settings.stoploss_points} pts`} testid="settings-stoploss-points" />
        <Row label="Cooldown" value={`${settings.signal_cooldown} min`} testid="settings-signal-cooldown" />
        <Row
          label="System"
          value={settings.system_enabled ? "ENABLED" : "DISABLED"}
          testid="settings-system-enabled"
          tone={settings.system_enabled ? "text-[#34D399]" : "text-[#F87171]"}
        />
      </div>
      <div className="mt-2 border-t border-[#1F2937] pt-2 font-mono text-[10px] text-[#8A99A8]">
        strategy {settings.strategy_version} · feature engine {settings.feature_engine_version} · config v
        {" "}{/* config_version rendered in header */}
      </div>
    </Panel>
  );
}
