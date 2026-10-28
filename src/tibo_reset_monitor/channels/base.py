"""Channel interface. One file per channel, registered in ``channels/__init__.py``."""

from __future__ import annotations

from typing import Protocol

from tibo_reset_monitor.core.http import TransportError
from tibo_reset_monitor.core.model import Notice, Receipt, Target


class Channel(Protocol):
    name: str
    targets: list[Target]

    def render(self, notice: Notice, target: Target) -> dict:
        """Turn a neutral notice into the payload this channel will POST."""

    def send(self, message: dict, target: Target, idempotency_key: str) -> Receipt:
        """Deliver one payload. Use ``idempotency_key`` for any provider dedupe field."""


def receipt_from_transport(exc: TransportError, *, sent: bool) -> Receipt:
    """A timeout after the request may have been accepted is ambiguous. Earlier failures are retried."""
    if sent and exc.ambiguous:
        return Receipt("ambiguous", detail=str(exc))
    return Receipt("retryable", detail=str(exc))
