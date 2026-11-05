"""Shared backend test fixtures."""

import pytest

from app import rate_limit


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    rate_limit.reset()
    yield
    rate_limit.reset()
