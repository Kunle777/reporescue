"""Shared backend test fixtures."""

import pytest
import urllib.error
import urllib.request

from app import rate_limit


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    rate_limit.reset()
    yield
    rate_limit.reset()


@pytest.fixture(autouse=True)
def _no_live_network(monkeypatch):
    """Fail fast on real network use; per-test mocks override this."""
    def _blocked(request, timeout=None):
        raise urllib.error.URLError("network disabled in tests")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
