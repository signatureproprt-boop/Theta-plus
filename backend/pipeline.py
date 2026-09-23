"""Phase D — runtime pipeline: feed -> normalizer -> feature engine -> signal
engine -> (Sheets writers + dashboard state).

This is the ONLY place live looping exists. The engines stay pure and
strategy-untouched; the pipeline is contained so one bad feed response never
crashes it (master prompt §39) and a Google outage never blocks a tick (§12).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

from data.dhan_client import DhanDataError, DhanMarketData
from alerts.service import AlertService
from paper.paper_engine import PaperTradingEngine
from execution.adapter import MockExecutionAdapter
from execution.dhan_execution import DhanExecutionAdapter
from execution.service import ExecutionService


def build_execution_adapter(config: AppConfig):
    """Mock by default. The live Dhan adapter requires BOTH explicit config and
    credentials; otherwise an inert (unconfigured) mock is used so no broker
    call is even possible."""
    if config.execution_adapter == "dhan":
        adapter = DhanExecutionAdapter()
        if adapter.configured:
            return adapter
    return MockExecutionAdapter(is_configured=False)
from data.sim_feed import day_bars
from data.normalizer import normalize_option_chain, normalize_tick
from engines.feature_engine import build_features
from engines.pcr_engine import pcr_trend_from_history
from engines.signal_engine import evaluate_and_apply
from engines.signal_memory import SignalMemory
from engines.vwap_engine import Bar
from lib.config import AppConfig, get_config
from lib.dates import IST, to_ist
from models.dashboard_models import SheetsStatusPanel, SystemLogEntry
from models.feature_models import MarketFeatures
from models.market_models import MarketSnapshot
from sheets.dashboard import build_dashboard, dashboard_grid
from sheets.google_sheets import GoogleSheetsAdapter
from sheets.settings import SheetSettingsError, settings_from_sheet
from sheets.writers import SheetsWriters

logger = logging.getLogger(__name__)


class SystemLog:
    """In-memory ring buffer of structured entries (§11); mirrored to Sheets."""

    def __init__(self, maxlen: int = 200) -> None:
        self._entries: deque[SystemLogEntry] = deque(maxlen=maxlen)

    def log(self, level: str, component: str, event: str, message: str) -> SystemLogEntry:
        entry = SystemLogEntry(
            timestamp=datetime.now(IST), level=level, component=component,
            event=event, message=message,
        )
        self._entries.append(entry)
        logger.info("%s %s %s %s", level, component, event, message)
        return entry

    def entries(self) -> list[SystemLogEntry]:
        return list(self._entries)

    def add_entry(self, entry: SystemLogEntry) -> None:
        """Append an already-built entry (used by the writer sink)."""
        self._entries.append(entry)


class SimFeed:
    """Simulated feed wrapper (clearly labeled); bars cached per session date."""

    name = "sim"

    def __init__(self, config: AppConfig, seed: str = "live") -> None:
        self.config = config
        self.seed = seed
        self._bars_cache: dict[str, list[Bar]] = {}

    def get_bars(self, date_iso: str) -> list[Bar]:
        if date_iso not in self._bars_cache:
            raw = day_bars(self.seed, date_iso)
            self._bars_cache[date_iso] = [
                Bar(high=b["high"], low=b["low"], close=b["close"], volume=float(b["volume"])) for b in raw
            ]
        return self._bars_cache[date_iso]

    async def get_snapshot(self, now: datetime) -> MarketSnapshot:
        from data.sim_feed import build_snapshot

        return build_snapshot(at=now, seed=self.seed)


class DhanFeed:
    """READ-ONLY Dhan feed: quote, option chain and genuine 1-minute candles."""

    name = "dhan"

    def __init__(self, config: AppConfig, fallback_interval: int = 50) -> None:
        self.config = config
        self.client = DhanMarketData()
        self._bars: dict[str, list[Bar]] = {}
        self._last_minute: dict[str, datetime] = {}
        self._fallback_interval = fallback_interval
        self._expiry = ""   # resolved from Dhan's official expiry list, never guessed

    def get_bars(self, date_iso: str) -> list[Bar]:
        return self._bars.get(date_iso, [])

    async def get_snapshot(self, now: datetime) -> MarketSnapshot:
        """One REAL Dhan snapshot: market quote + option chain for the expiry
        returned by Dhan's own expiry list. Payload mapping lives in
        data/dhan_mapper.py; strategy code is untouched."""
        from data.dhan_mapper import DhanPayloadError, map_index_quote, map_option_chain

        try:
            if self._expiry and self._expiry < now.date().isoformat():
                # The client's expiry list is cached too; refresh it at rollover.
                await self.client.get_expiry_list()
                self._expiry = ""
            expiry = self._expiry or await self.client.get_nearest_expiry(on=now.date())
            self._expiry = expiry
            quote = await self.client.get_index_quote()
            chain_raw = await self.client.get_option_chain(expiry=expiry)
            tick = normalize_tick(map_index_quote(quote, now=now), source="dhan")
            chain = normalize_option_chain(
                map_option_chain(chain_raw, expiry=expiry, now=now),
                fallback_interval=self._fallback_interval,
                source="dhan",
            )
        except DhanPayloadError as exc:
            # Contract mismatch / missing fields: surface as a data error so the
            # engine answers WAIT instead of trading on a guess.
            raise DhanDataError(f"Dhan payload rejected: {exc}") from None

        date_iso = now.date().isoformat()
        minute = now.replace(second=0, microsecond=0)
        if self._last_minute.get(date_iso) != minute:
            start = minute.replace(hour=9, minute=15)
            raw = await self.client.get_index_minute_candles(start, minute)
            from data.dhan_mapper import map_minute_candles
            bars, latest = map_minute_candles(raw, start, minute)
            if latest < minute - timedelta(minutes=2):
                raise DhanDataError("minute candles are stale; signal blocked")
            self._bars[date_iso] = bars
            self._last_minute[date_iso] = minute
        return MarketSnapshot(
            snapshot_id=f"dhan-{int(now.timestamp())}",
            timestamp=now,
            instrument="NIFTY",
            tick=tick,
            chain=chain,
            source="dhan",
        )


def build_feed(config: AppConfig):
    if config.data_source == "dhan":
        if not DhanMarketData().configured:
            raise DhanDataError("data_source=dhan requires DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID")
        return DhanFeed(config)
    return SimFeed(config)


class Pipeline:
    """Live pipeline + control-room state. All failures contained."""

    def __init__(self, config: Optional[AppConfig] = None) -> None:
        self.config = config or get_config()
        self.memory = SignalMemory()
        self.system_log = SystemLog()
        self.writers = SheetsWriters(self.config.sheets_flush_seconds, log_sink=self._writer_log)
        try:
            self.adapter = GoogleSheetsAdapter()
        except Exception as exc:  # invalid credentials in env: stay disabled
            self.adapter = GoogleSheetsAdapter(spreadsheet_id="", service_account_spec="{}")
            self.system_log.log("ERROR", "GOOGLE_SHEETS", "SHEETS_CONFIG_ERROR", str(exc))
        self.feed = build_feed(self.config)
        self.features: Optional[MarketFeatures] = None
        self.decision = None
        self.sheets_last_error = ""
        # Phase F: chart marker history (visualization only). Every decision is
        # kept, including WAIT; the API hides WAIT by default.
        self.chart_markers: deque = deque(maxlen=500)
        # Phase G: alerts + PAPER trading. Both are strictly downstream of the
        # frozen signal engine and both fail closed without touching it.
        self.alerts = AlertService(
            enabled=self.config.alerts_enabled,
            mode=self.config.system_mode,
            log_sink=self.system_log.log,
        )
        self.paper = PaperTradingEngine(self.config, data_origin="LIVE")
        # Phase H: additive execution/risk layer. Defaults: RESEARCH mode, live
        # execution DISABLED + DISARMED, Mock adapter unless explicitly configured.
        self.execution = ExecutionService(self.config, adapter=build_execution_adapter(self.config))
        self._pcr_hist: deque = deque(maxlen=self.config.pcr_lookback + 30)
        self._atm_pcr_hist: deque = deque(maxlen=self.config.pcr_lookback + 30)
        self._prev_snapshot: Optional[MarketSnapshot] = None
        self._tasks: list[asyncio.Task] = []

    def _writer_log(self, entry: SystemLogEntry) -> None:
        self.system_log.add_entry(entry)  # share the ring, avoid recursion

    # --- Phase G gates ---
    @property
    def paper_enabled(self) -> bool:
        """PAPER mode, or RESEARCH mode with research simulation explicitly on."""
        return self.config.system_mode == "PAPER" or self.config.paper_trading_enabled

    def _session_over(self, now: datetime) -> bool:
        from lib.dates import parse_hhmm

        return now.timetz().replace(tzinfo=None) > parse_hhmm(self.config.market_close)

    # --- one deterministic iteration (feed -> features -> decision -> queues) ---
    async def tick(self, now: Optional[datetime] = None):
        now = to_ist(now or datetime.now(IST), self.config.timezone)
        try:
            snapshot = await self.feed.get_snapshot(now)
        except Exception as exc:
            self.system_log.log("ERROR", "DHAN", "DHAN_CONNECTION", f"feed failed: {exc}")
            # Do not keep displaying an actionable setup from the last good tick.
            if self.decision is not None:
                self.decision = self.decision.model_copy(update={
                    "decision": "WAIT", "data_health": "STALE",
                    "reasons": (f"DATA_FEED_FAILED: {type(exc).__name__}",),
                })
            if self.features is not None:
                health = self.features.data_health
                self.features = self.features.model_copy(update={
                    "data_health": health.model_copy(update={
                        "status": type(health.status).STALE,
                        "checks": {**health.checks, "DATA_AGE": "STALE"},
                        "details": (*health.details, "data feed failed"),
                    }),
                })
            return self.dashboard()

        date_iso = now.date().isoformat()
        all_bars = self.feed.get_bars(date_iso)
        minute = now.replace(second=0, microsecond=0)
        # bars are a 1-min series from market open: slice to the current minute
        open_minute = minute.replace(hour=9, minute=15)
        count = max(int((minute - open_minute).total_seconds() // 60) + 1, 2)
        bars = all_bars[:count]

        features = build_features(
            snapshot, self.config, bars=bars,
            pcr_history=list(self._pcr_hist), prev_snapshot=self._prev_snapshot,
        )
        if features.pcr.total_pcr is not None:
            self._pcr_hist.append((features.timestamp, features.pcr.total_pcr))
        if features.pcr.atm_pcr is not None:
            self._atm_pcr_hist.append((features.timestamp, features.pcr.atm_pcr))
        atm_pcr_change = None
        if len(self._atm_pcr_hist) >= 2 and self._atm_pcr_hist[-1][1] is not None and self._atm_pcr_hist[-2][1] is not None:
            atm_pcr_change = round(self._atm_pcr_hist[-1][1] - self._atm_pcr_hist[-2][1], 4)
        atm_pcr_trend = pcr_trend_from_history(
            list(self._atm_pcr_hist), self.config.pcr_lookback, self.config.pcr_flat_band
        ).value

        decision = evaluate_and_apply(self.memory, features, self.config, now)
        self.features, self.decision = features, decision
        self._prev_snapshot = snapshot

        self.system_log.log("INFO", "SIGNAL_ENGINE", "SIGNAL_EVALUATED",
                            f"{decision.decision} (CE {decision.ce_score}/100, PE {decision.pe_score}/100)")
        if decision.decision in ("CE_SETUP", "PE_SETUP"):
            self.system_log.log("INFO", "SIGNAL_ENGINE", "SIGNAL_GENERATED", decision.decision)

        # Phase F — record the chart marker for this decision (serialization only).
        from chart.markers import link_invalidations, marker_from_decision

        chart_marker = marker_from_decision(decision, features)
        if self.feed.name == "dhan":
            chart_marker = chart_marker.model_copy(update={"data_origin_label": "LIVE • DHAN DATA"})
        self.chart_markers.append(chart_marker)
        marker = link_invalidations(list(self.chart_markers))[-1]

        # Phase G — alerts (contained: a Telegram outage never reaches the engines).
        if self.config.alerts_enabled:
            try:
                await self.alerts.dispatch(marker)
            except Exception as exc:  # defence in depth; the service already contains
                self.system_log.log("ERROR", "ALERTS", "ALERT_FAILED", f"{type(exc).__name__}: {exc}")

        # Phase G — PAPER trading (opt-in; ZERO broker execution exists).
        if self.paper_enabled:
            try:
                self.paper.on_tick(
                    decision, features, snapshot, marker.signal_id, now=now,
                    expired=self._session_over(now),
                )
            except Exception as exc:
                self.system_log.log("ERROR", "PAPER", "PAPER_ERROR", f"{type(exc).__name__}: {exc}")

        # Phase H — LIVE execution attempt. Runs ONLY in LIVE mode; the gate then
        # re-checks enabled/armed/kill-switch/risk/reconciliation and blocks by
        # default. Fully contained: an execution error never reaches the engines.
        if self.config.system_mode == "LIVE":
            try:
                await self.execution.attempt_entry(decision, features, marker.signal_id, now=now)
            except Exception as exc:
                self.system_log.log("ERROR", "EXECUTION", "EXECUTION_ERROR", f"{type(exc).__name__}: {exc}")

        self.writers.queue_raw(features, snapshot)
        self.writers.queue_pcr(features, atm_pcr_change, atm_pcr_trend)
        self.writers.queue_signal(decision, features)
        return self.dashboard(atm_pcr_change=atm_pcr_change, atm_pcr_trend=atm_pcr_trend)

    # --- control-room surfaces ---
    def sheets_status(self) -> SheetsStatusPanel:
        return SheetsStatusPanel(
            enabled=self.adapter.enabled and self.config.sheets_enabled,
            configured=self.adapter.configured,
            last_error=self.writers.last_error,
            rows_written=self.writers.rows_written,
        )

    def dashboard(self, **kwargs) -> dict:
        return build_dashboard(
            features=self.features,
            decision=self.decision,
            config=self.config,
            data_source=self.feed.name,
            memory=self.memory.snapshot(),
            sheets_status=self.sheets_status(),
            log_entries=self.system_log.entries()[-25:],
            atm_pcr_change=kwargs.get("atm_pcr_change"),
            atm_pcr_trend=kwargs.get("atm_pcr_trend", "INSUFFICIENT_DATA"),
        )

    # --- Sheets flush + settings pull (batched, contained) ---
    async def flush_sheets(self, force: bool = False) -> int:
        written = await self.writers.flush(self.adapter, force=force)
        if written:
            self.system_log.log("INFO", "GOOGLE_SHEETS", "SHEETS_FLUSHED", f"{written} rows written")
        await self._pull_sheet_settings()
        return written

    async def _pull_sheet_settings(self) -> None:
        if not (self.adapter.enabled and self.config.sheets_enabled):
            return
        try:
            values = await self.adapter.read_key_values("SETTINGS")
            if values:
                new_config = settings_from_sheet(self.config, values)
                if new_config is not self.config:
                    self.config = new_config
                    self.writers.flush_seconds = new_config.sheets_flush_seconds
                    self.system_log.log("INFO", "SETTINGS", "SETTINGS_APPLIED",
                                        "sheet settings validated and applied")
        except SheetSettingsError as exc:
            self.system_log.log("WARN", "SETTINGS", "SETTINGS_REJECTED", str(exc))
        except Exception as exc:
            self.system_log.log("ERROR", "GOOGLE_SHEETS", "SHEETS_ERROR", f"settings read failed: {exc}")

    async def mirror_dashboard(self) -> None:
        if not (self.adapter.enabled and self.config.sheets_enabled):
            return
        try:
            await self.adapter.write_grid("DASHBOARD", dashboard_grid(self.dashboard()))
        except Exception as exc:
            self.writers.last_error = str(exc)
            self.system_log.log("ERROR", "GOOGLE_SHEETS", "SHEETS_ERROR", f"dashboard mirror failed: {exc}")

    # --- background loops (never crash) ---
    async def _tick_loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.system_log.log("ERROR", "PIPELINE", "TICK_ERROR", f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(self.config.pipeline_interval_seconds)

    async def _flush_loop(self) -> None:
        while True:
            try:
                await self.flush_sheets()
                await self.mirror_dashboard()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.system_log.log("ERROR", "GOOGLE_SHEETS", "SHEETS_ERROR", f"flush loop: {exc}")
            await asyncio.sleep(self.config.sheets_flush_seconds)

    def start(self) -> None:
        if not self._tasks:
            self._tasks = [asyncio.create_task(self._tick_loop()), asyncio.create_task(self._flush_loop())]
            self._tasks.append(asyncio.create_task(self._startup_reconcile()))
            self.system_log.log("INFO", "PIPELINE", "DATA_CONNECTED",
                                f"pipeline started (feed={self.feed.name}, interval={self.config.pipeline_interval_seconds}s)")

    async def _startup_reconcile(self) -> None:
        """Phase H §38: live execution stays blocked until reconciliation completes."""
        try:
            state = await self.execution.startup_reconcile()
            self.system_log.log("INFO", "EXECUTION", "RECONCILIATION",
                                f"startup reconciliation: {state}")
        except Exception as exc:
            self.system_log.log("ERROR", "EXECUTION", "RECONCILIATION",
                                f"startup reconciliation failed: {type(exc).__name__}")

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []


_pipeline: Optional[Pipeline] = None


def get_pipeline() -> Pipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = Pipeline()
    return _pipeline
