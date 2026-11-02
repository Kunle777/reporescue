"""Request timing middleware: log line, latency header, app wiring.

Drives the pure-ASGI middleware directly (no TestClient/httpx in this repo).
"""

import logging
import re

import pytest

from app import main as main_module
from app.request_logging import TIMING_HEADER, RequestTimingMiddleware


def drive(coro):
    """Run an I/O-free coroutine chain to completion without an event loop.

    The middleware only awaits plain coroutines (no sockets, sleeps, or
    futures), so a single send() either finishes it or proves it needs a
    real loop.
    """

    try:
        yielded = coro.send(None)
    except StopIteration:
        return
    coro.close()
    raise AssertionError(f"middleware yielded unexpectedly: {yielded!r}")


def ok_app(status=200):
    async def app(scope, receive, send):
        await send(
            {"type": "http.response.start", "status": status, "headers": []}
        )
        await send({"type": "http.response.body", "body": b"{}"})

    return app


def run_middleware(downstream, method="GET", path="/ping", query=b""):
    middleware = RequestTimingMiddleware(downstream)
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query,
        "headers": [],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    drive(middleware(scope, receive, send))
    return sent


def response_headers(sent):
    start = next(m for m in sent if m["type"] == "http.response.start")
    return {k.decode("latin-1"): v.decode("latin-1") for k, v in start["headers"]}


def test_success_logs_timestamp_and_latency(caplog):
    with caplog.at_level(logging.INFO, logger="reporescue.api"):
        run_middleware(ok_app())
    assert len(caplog.records) == 1
    line = caplog.records[0].getMessage()
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z ", line)
    assert "method=GET" in line
    assert "path=/ping" in line
    assert "status=200" in line
    assert float(line.split("duration_ms=")[1]) >= 0


def test_latency_header_present():
    sent = run_middleware(ok_app())
    assert float(response_headers(sent)[TIMING_HEADER.lower()]) >= 0


def test_query_string_never_logged(caplog):
    with caplog.at_level(logging.INFO, logger="reporescue.api"):
        run_middleware(ok_app(), query=b"token=secret-value")
    assert "secret-value" not in caplog.records[0].getMessage()


def test_error_responses_logged_with_status(caplog):
    with caplog.at_level(logging.INFO, logger="reporescue.api"):
        run_middleware(ok_app(status=500))
    line = caplog.records[0].getMessage()
    assert "status=500" in line
    assert "duration_ms=" in line


def test_exception_still_logs_then_propagates(caplog):
    async def boom(scope, receive, send):
        raise RuntimeError("kaboom")

    with caplog.at_level(logging.INFO, logger="reporescue.api"):
        with pytest.raises(RuntimeError, match="kaboom"):
            run_middleware(boom)
    assert "status=?" in caplog.records[0].getMessage()


def test_main_app_has_timing_middleware():
    assert any(
        entry.cls is RequestTimingMiddleware
        for entry in main_module.app.user_middleware
    )
