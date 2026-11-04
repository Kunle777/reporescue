"""Per-request timing logs for latency measurement.

Logs one line per API request: UTC start timestamp, method, path, status,
and handler duration in milliseconds. Path only, never the query string,
so tokens stay out of the logs.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("reporescue.api")

TIMING_HEADER = "X-Response-Time-Ms"


class RequestTimingMiddleware:
    """Pure-ASGI middleware: time the request, log it, add a header."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = datetime.now(timezone.utc)
        start_perf = time.perf_counter()
        status_holder: dict[str, int] = {}

        async def send_with_timing(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = list(message.get("headers", []))
                # Duration is measured up to response start (headers sent);
                # body streaming time is excluded.
                elapsed_ms = (time.perf_counter() - start_perf) * 1000
                headers.append(
                    (
                        TIMING_HEADER.lower().encode("latin-1"),
                        f"{elapsed_ms:.1f}".encode("latin-1"),
                    )
                )
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_timing)
        finally:
            elapsed_ms = (time.perf_counter() - start_perf) * 1000
            logger.info(
                "%s method=%s path=%s status=%s duration_ms=%.1f",
                started.isoformat(timespec="milliseconds").replace(
                    "+00:00", "Z"
                ),
                scope.get("method", "?"),
                scope.get("path", "?"),
                status_holder.get("status", "?"),
                elapsed_ms,
            )
