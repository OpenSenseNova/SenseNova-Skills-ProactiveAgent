"""Thin delivery boundary from ACP-mapped events to the V1 Core API."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from typing import Any

from proactive_memory_service.contracts import InboundEvent


class EventDeliveryError(RuntimeError):
    """Raised when strict V1 event delivery cannot reach the Core API."""


class V1HttpEventSink:
    """POST typed inbound events to the existing Harness-neutral V1 routes.

    Connector failures are fail-open by default so a memory-service outage does
    not block the user's Agent turn. Tests and controlled jobs can set
    ``fail_open=False`` when delivery must be asserted synchronously.
    """

    def __init__(
        self,
        service_url: str,
        *,
        timeout_seconds: float = 1.5,
        fail_open: bool = True,
        opener: Callable[..., Any] = urllib.request.urlopen,
        log: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(service_url, str) or not service_url.strip():
            raise ValueError("service_url must be a non-empty string")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.service_url = service_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.fail_open = fail_open
        self.opener = opener
        self.log = log or (lambda _message: None)

    def __call__(self, event: InboundEvent) -> None:
        event_type = event.event_type.value
        request = urllib.request.Request(
            f"{self.service_url}/v1/events/{event_type}",
            data=json.dumps(event.to_payload(), ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                response.read()
        except Exception as exc:
            error = EventDeliveryError(
                f"failed to deliver {event_type} to Proactive Memory: {exc}"
            )
            self.log(str(error))
            if not self.fail_open:
                raise error from exc
