"""Phase G — alert records (provider-independent).

SECURITY: an AlertRecord NEVER stores a bot token, chat id or any credential —
only the provider name, the alert type and a redacted error string.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

ALERT_CE_SETUP = "CE_SETUP"
ALERT_PE_SETUP = "PE_SETUP"
ALERT_INVALIDATED = "INVALIDATED"

STATUS_SENT = "SENT"
STATUS_FAILED = "FAILED"
STATUS_DUPLICATE = "DUPLICATE_SUPPRESSED"
STATUS_NOT_CONFIGURED = "NOT_CONFIGURED"
STATUS_DISABLED = "DISABLED"


class AlertRecord(BaseModel):
    """One ALERT_LOG row. Immutable and credential-free."""

    model_config = ConfigDict(frozen=True)

    alert_id: str
    signal_id: str
    timestamp: datetime
    provider: str  # e.g. "telegram"
    alert_type: str  # CE_SETUP | PE_SETUP | INVALIDATED
    status: str
    error: str = ""
    message_preview: str = ""  # first line only; never contains credentials


class AlertProviderStatus(BaseModel):
    """Read-only provider status for the dashboard/API."""

    model_config = ConfigDict(frozen=True)

    provider: str = "telegram"
    configured: bool = False
    status: str = STATUS_NOT_CONFIGURED  # CONFIGURED | NOT_CONFIGURED | DISABLED
    last_error: str = ""
    sent: int = 0
    failed: int = 0
    suppressed_duplicates: int = 0
    note: str = ""
    delivery: str = "PENDING"  # CONFIGURED | PENDING — real delivery is never claimed
    real_delivery_verified: bool = False


class PaperSummaryUnavailable(BaseModel):
    """Placeholder kept out of this module on purpose (see paper_models)."""

    model_config = ConfigDict(frozen=True)

    reason: str = ""
    detail: Optional[str] = None
