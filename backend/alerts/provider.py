"""Phase G — provider-independent alert interface.

Adding another channel later (e.g. a different messenger) means implementing
this protocol only; nothing else in the system changes. WhatsApp stays out of
V1 by design.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class AlertProvider(Protocol):
    """One delivery channel. Implementations must never raise on config absence."""

    name: str

    @property
    def configured(self) -> bool:
        """True only when every required credential is present in the env."""
        ...

    async def send(self, message: str) -> None:
        """Deliver `message`. Raises on delivery failure; the service contains it."""
        ...


class NullProvider:
    """Explicitly disabled channel: reports not-configured, never sends."""

    name = "null"

    @property
    def configured(self) -> bool:
        return False

    async def send(self, message: str) -> None:  # pragma: no cover - never called
        raise RuntimeError("no alert provider configured")
