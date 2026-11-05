"""Persistent daily quota tests; no provider calls or external services."""
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.usage_quota import (
    AI_DAILY_USER_LIMIT,
    DEBUG_DAILY_USER_LIMIT,
    GLOBAL_AI_DAILY_LIMIT,
    UsageLimitExceeded,
    reserve_operation,
    usage_status,
)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path.as_posix()}/quota.db")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE daily_usage_counters (
                scope_key VARCHAR(100) NOT NULL,
                usage_date DATE NOT NULL,
                quota_type VARCHAR(20) NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (scope_key, usage_date, quota_type),
                CHECK (used >= 0)
            )
        """))
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_ai_quota_is_ten_per_user_per_lagos_day(db):
    for _ in range(AI_DAILY_USER_LIMIT):
        reserve_operation(db, user_id="user-a")
    with pytest.raises(UsageLimitExceeded) as exc:
        reserve_operation(db, user_id="user-a")
    assert exc.value.code == "AI_DAILY_USER_LIMIT_REACHED"


def test_debug_quota_is_three_and_independent_of_ai_quota(db):
    for _ in range(DEBUG_DAILY_USER_LIMIT):
        reserve_operation(db, user_id="user-a", include_debug=True)
    with pytest.raises(UsageLimitExceeded) as exc:
        reserve_operation(db, user_id="user-a", include_debug=True)
    assert exc.value.code == "BETA_DAILY_DEBUG_LIMIT_REACHED"
    assert usage_status(db, user_id="user-a")["ai"]["used"] == DEBUG_DAILY_USER_LIMIT


def test_ai_quota_is_per_user_but_global_budget_is_shared(db, monkeypatch):
    monkeypatch.setattr("app.usage_quota.GLOBAL_AI_DAILY_LIMIT", 2)
    reserve_operation(db, user_id="user-a")
    reserve_operation(db, user_id="user-b")
    with pytest.raises(UsageLimitExceeded) as exc:
        reserve_operation(db, user_id="user-c")
    assert exc.value.code == "GLOBAL_AI_DAILY_LIMIT_REACHED"


def test_failed_multi_counter_reservation_rolls_back_partial_usage(db):
    for _ in range(DEBUG_DAILY_USER_LIMIT):
        reserve_operation(db, user_id="user-a", include_debug=True)
    before = usage_status(db, user_id="user-a")
    with pytest.raises(UsageLimitExceeded):
        reserve_operation(db, user_id="user-a", include_debug=True)
    after = usage_status(db, user_id="user-a")
    assert after == before


def test_ai_usage_status_never_exposes_global_counter(db):
    status = usage_status(db, user_id="user-a")
    assert status["timezone"] == "Africa/Lagos"
    assert status["ai"]["limit"] == 10
    assert status["debug"]["limit"] == 3
    assert "global" not in str(status).lower()
