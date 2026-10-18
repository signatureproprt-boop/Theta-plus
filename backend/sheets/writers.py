"""Phase D — batched, throttled sheet writers.

Queues are filled by the pipeline on every tick (cheap, in-memory) and
flushed to Google Sheets in BATCHES on a controlled interval (§7/§12/§16):
one append call per tab per flush, never per tick, never per row.

Every flush failure is caught, logged (SYSTEM_LOG -> SHEETS_ERROR) and never
propagates — a Google outage must not touch the signal engine (§12).
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any, Optional

from models.dashboard_models import SystemLogEntry
from models.feature_models import MarketFeatures
from models.market_models import MarketSnapshot
from models.signal_models import SignalDecision

RAW_HEADERS = [
    "timestamp", "instrument", "spot", "open", "high", "low", "close", "volume", "vwap",
    "atm", "strike", "ce_ltp", "ce_oi", "ce_oi_change", "ce_volume", "ce_iv",
    "pe_ltp", "pe_oi", "pe_oi_change", "pe_volume", "pe_iv", "data_health",
]
PCR_HEADERS = [
    "timestamp", "total_put_oi", "total_call_oi", "total_pcr",
    "atm_put_oi", "atm_call_oi", "atm_pcr", "pcr_change", "atm_pcr_change",
    "pcr_trend", "atm_pcr_trend",
]
SIGNAL_HEADERS = [
    "timestamp", "instrument", "spot", "vwap", "vwap_distance", "atm",
    "total_pcr", "atm_pcr", "pcr_change", "pcr_trend",
    "put_oi", "call_oi", "put_oi_change", "call_oi_change",
    "ce_score", "ce_max_score", "pe_score", "pe_max_score",
    "decision", "state", "reason", "strategy_version", "feature_engine_version", "data_health",
]
TRADE_HEADERS = [
    "date", "time", "signal", "instrument", "strike", "option_type", "entry", "target",
    "stoploss", "exit", "points", "quantity", "gross_pnl", "charges", "net_pnl",
    "exit_reason", "signal_reason", "strategy_version",
]
SYSTEM_HEADERS = ["timestamp", "level", "component", "event", "message"]

TAB_HEADERS = {
    "RAW_DATA": RAW_HEADERS,
    "PCR_DATA": PCR_HEADERS,
    "SIGNAL": SIGNAL_HEADERS,
    "TRADE_LOG": TRADE_HEADERS,
    "SYSTEM_LOG": SYSTEM_HEADERS,
}
# TRADE_LOG is marked as research-only in its first written row (§10).
TRADE_BANNER = ["PAPER / RESEARCH TRADE LOG — NOT BROKER EXECUTION (V1 signal-only)"]


def _fmt_num(v, digits: int = 2):
    return round(float(v), digits) if v is not None else ""


def raw_rows(features: MarketFeatures, snapshot: MarketSnapshot) -> list[list[Any]]:
    """One row per strike in the ATM window (§7 columns), batched per snapshot."""
    by_strike = {r.strike: r for r in snapshot.chain.rows}
    tick = snapshot.tick
    rows = []
    for strike in features.atm.strikes:
        row = by_strike.get(strike)
        ce = row.ce if row else None
        pe = row.pe if row else None
        rows.append([
            features.timestamp.isoformat(), features.symbol, _fmt_num(features.spot),
            _fmt_num(tick.open), _fmt_num(tick.high), _fmt_num(tick.low), _fmt_num(tick.close), tick.volume,
            _fmt_num(features.vwap.vwap), features.atm.atm_strike, strike,
            _fmt_num(ce.ltp) if ce else "", ce.oi if ce else "", ce.oi_change if ce else "",
            ce.volume if ce else "", _fmt_num(ce.iv) if ce and ce.iv is not None else "",
            _fmt_num(pe.ltp) if pe else "", pe.oi if pe else "", pe.oi_change if pe else "",
            pe.volume if pe else "", _fmt_num(pe.iv) if pe and pe.iv is not None else "",
            features.data_health.status.value,
        ])
    return rows


def pcr_row(features: MarketFeatures, atm_pcr_change: Optional[float], atm_pcr_trend: str) -> list[Any]:
    f = features
    return [
        f.timestamp.isoformat(), f.pcr.total_put_oi, f.pcr.total_call_oi, _fmt_num(f.pcr.total_pcr, 4),
        f.pcr.atm_put_oi, f.pcr.atm_call_oi, _fmt_num(f.pcr.atm_pcr, 4),
        _fmt_num(f.pcr.pcr_change, 4), _fmt_num(atm_pcr_change, 4),
        f.pcr.pcr_trend.value, atm_pcr_trend,
    ]


def signal_row(decision: SignalDecision, features: MarketFeatures) -> list[Any]:
    return [
        decision.timestamp.isoformat(), decision.symbol, _fmt_num(decision.spot), _fmt_num(decision.vwap),
        _fmt_num(decision.vwap - decision.spot, 2) if decision.vwap is not None and decision.spot is not None else "",
        decision.atm if decision.atm is not None else "",
        _fmt_num(decision.pcr, 4),
        _fmt_num(features.pcr.atm_pcr, 4),
        _fmt_num(features.pcr.pcr_change, 4),
        decision.pcr_trend or "",
        features.oi.put_oi, features.oi.call_oi,
        features.oi.put_oi_change if features.oi.put_oi_change is not None else "",
        features.oi.call_oi_change if features.oi.call_oi_change is not None else "",
        decision.ce_score, decision.ce_max_score, decision.pe_score, decision.pe_max_score,
        decision.decision.replace("_", " "), decision.state,
        " | ".join(decision.reasons),
        decision.strategy_version, decision.feature_engine_version, decision.data_health,
    ]


class SheetsWriters:
    """Per-tab row queues + throttled batch flush. Signal-engine independent."""

    def __init__(self, flush_seconds: int = 30, log_sink=None) -> None:
        self.flush_seconds = flush_seconds
        self._log = log_sink  # callable(SystemLogEntry)
        self._queues: dict[str, deque] = {
            t: deque() for t in ("RAW_DATA", "PCR_DATA", "SIGNAL", "TRADE_LOG", "SYSTEM_LOG")
        }
        self._headers_done: set[str] = set()
        self._last_flush: Optional[datetime] = None
        self.rows_written = 0
        self.last_error = ""

    # --- queueing (called by the pipeline every tick; never touches network) ---
    def queue_raw(self, features: MarketFeatures, snapshot: MarketSnapshot) -> None:
        self._queues["RAW_DATA"].extend(raw_rows(features, snapshot))

    def queue_pcr(self, features: MarketFeatures, atm_pcr_change: Optional[float], atm_pcr_trend: str) -> None:
        self._queues["PCR_DATA"].append(pcr_row(features, atm_pcr_change, atm_pcr_trend))

    def queue_signal(self, decision: SignalDecision, features: MarketFeatures) -> None:
        self._queues["SIGNAL"].append(signal_row(decision, features))

    def queue_trade(self, row: list[Any]) -> None:
        """PAPER / RESEARCH rows only — no broker order ids exist in V1 (§10/§22)."""
        self._queues["TRADE_LOG"].append(row)

    def queue_log(self, entry: SystemLogEntry) -> None:
        self._queues["SYSTEM_LOG"].append([
            entry.timestamp.isoformat(), entry.level, entry.component, entry.event, entry.message,
        ])

    def pending(self, tab: str) -> int:
        return len(self._queues[tab])

    # --- flushing (controlled interval; every failure contained here) ---
    async def flush(self, adapter, force: bool = False) -> int:
        """Flush all queues in batches. Returns rows written; NEVER raises."""
        now = datetime.now()
        if not force and self._last_flush and (now - self._last_flush).total_seconds() < self.flush_seconds:
            return 0
        self._last_flush = now
        if not adapter.enabled:
            for q in self._queues.values():
                q.clear()  # adapter off: drop rows rather than grow unbounded
            return 0
        written = 0
        for tab, queue in list(self._queues.items()):
            if not queue:
                continue
            batch: list[list[Any]] = []
            if tab not in self._headers_done:
                if tab == "TRADE_LOG":
                    batch.append(TRADE_BANNER)
                batch.append(TAB_HEADERS[tab])
                self._headers_done.add(tab)
            batch.extend(list(queue))
            queue.clear()
            try:
                written += await adapter.append_rows(tab, batch)
            except Exception as exc:  # Sheets failure: log and continue (§12)
                self.last_error = f"{type(exc).__name__}: {exc}"
                if self._log:
                    self._log(SystemLogEntry(
                        timestamp=now, level="ERROR", component="GOOGLE_SHEETS",
                        event="SHEETS_ERROR", message=f"flush failed for {tab}: {self.last_error}",
                    ))
        self.rows_written += written
        return written
