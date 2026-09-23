"""Phase G — alert service: dedupe, deliver, record. Failures are contained.

    Signal Engine -> [decision] -> AlertService -> AlertProvider -> Telegram

The service NEVER raises into the caller: a Telegram outage records
ALERT_FAILED and the signal engine, paper engine, replay and dashboard all
continue (§8/§31).
"""

from __future__ import annotations

import uuid
from collections import deque
from datetime import datetime
from typing import Callable, Optional

from alerts.formatter import alert_type_for, format_alert
from alerts.provider import AlertProvider
from alerts.telegram import TelegramProvider
from lib.dates import IST
from models.alert_models import (
    STATUS_DISABLED,
    STATUS_DUPLICATE,
    STATUS_FAILED,
    STATUS_NOT_CONFIGURED,
    STATUS_SENT,
    AlertProviderStatus,
    AlertRecord,
)
from models.chart_models import ChartSignalMarker


class AlertService:
    """Provider-independent alert dispatcher with a credential-free ALERT_LOG."""

    def __init__(
        self,
        provider: Optional[AlertProvider] = None,
        enabled: bool = True,
        mode: str = "RESEARCH",
        log_sink: Optional[Callable[[str, str, str, str], None]] = None,
        maxlen: int = 300,
    ) -> None:
        self.provider = provider or TelegramProvider()
        self.enabled = enabled
        self.mode = mode
        self._log_sink = log_sink
        self._records: deque[AlertRecord] = deque(maxlen=maxlen)
        self._sent_keys: set[tuple[str, str]] = set()  # (signal_id, alert_type)
        self.last_error = ""

    # --- log ---
    def records(self) -> list[AlertRecord]:
        return list(self._records)

    def status(self) -> AlertProviderStatus:
        records = self._records
        configured = bool(getattr(self.provider, "configured", False))
        if not self.enabled:
            state = STATUS_DISABLED
        elif configured:
            state = "CONFIGURED"
        else:
            state = STATUS_NOT_CONFIGURED
        return AlertProviderStatus(
            provider=getattr(self.provider, "name", "unknown"),
            configured=configured,
            status=state,
            last_error=self.last_error,
            sent=sum(1 for r in records if r.status == STATUS_SENT),
            failed=sum(1 for r in records if r.status == STATUS_FAILED),
            suppressed_duplicates=sum(1 for r in records if r.status == STATUS_DUPLICATE),
            note=(
                "Telegram credentials absent — alerts are recorded as NOT_CONFIGURED and "
                "the signal engine continues normally."
                if not configured
                else "Credentials present. Real end-to-end delivery is only proven by a live send."
            ),
            delivery="CONFIGURED" if configured else "PENDING",
            real_delivery_verified=False,
        )

    def _record(self, marker: ChartSignalMarker, alert_type: str, status: str,
                error: str = "", preview: str = "") -> AlertRecord:
        record = AlertRecord(
            alert_id=str(uuid.uuid4()),
            signal_id=marker.signal_id,
            timestamp=datetime.now(IST),
            provider=getattr(self.provider, "name", "unknown"),
            alert_type=alert_type,
            status=status,
            error=error,
            message_preview=preview,
        )
        self._records.append(record)
        if self._log_sink:
            level = "ERROR" if status == STATUS_FAILED else "INFO"
            event = "ALERT_FAILED" if status == STATUS_FAILED else f"ALERT_{status}"
            self._log_sink(level, "ALERTS", event, f"{alert_type} {marker.signal_id}: {status} {error}".strip())
        return record

    # --- dispatch ---
    async def dispatch(self, marker: ChartSignalMarker) -> Optional[AlertRecord]:
        """Alert one marker. Returns None when the marker is not alertable."""
        alert_type = alert_type_for(marker.decision)
        if alert_type is None:
            return None  # WAIT is never alerted (§4)
        key = (marker.signal_id, alert_type)

        if not self.enabled:
            return self._record(marker, alert_type, STATUS_DISABLED)
        if key in self._sent_keys:
            return self._record(marker, alert_type, STATUS_DUPLICATE)
        if not getattr(self.provider, "configured", False):
            # No credentials: log it once per signal and keep the engine running.
            self._sent_keys.add(key)
            return self._record(marker, alert_type, STATUS_NOT_CONFIGURED)

        message = format_alert(marker, self.mode)
        preview = message.splitlines()[0] if message else ""
        try:
            await self.provider.send(message)
        except Exception as exc:
            redact = getattr(self.provider, "redact", None)
            error = redact(str(exc)) if callable(redact) else str(exc)
            self.last_error = error
            # Failure does NOT mark the alert as sent: a later retry is allowed,
            # but it never blocks or crashes the caller.
            return self._record(marker, alert_type, STATUS_FAILED, error=error, preview=preview)

        self._sent_keys.add(key)
        return self._record(marker, alert_type, STATUS_SENT, preview=preview)
