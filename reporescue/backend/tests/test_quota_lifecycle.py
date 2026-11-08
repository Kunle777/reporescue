"""Quota refund lifecycle: failed operations must not consume usage.

Covers ``release_operation`` (the mirror of ``reserve_operation``):
refunds are floored at zero, scoped per counter, and safe to call
when nothing was reserved.
"""
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.usage_quota import (
    AI_DAILY_USER_LIMIT,
    DEBUG_DAILY_USER_LIMIT,
    UsageLimitExceeded,
    release_operation,
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


def test_release_returns_user_and_global_slots(db):
    reserve_operation(db, user_id="user-a", include_debug=True)
    assert usage_status(db, user_id="user-a")["ai"]["used"] == 1
    release_operation(db, user_id="user-a", include_debug=True)
    status = usage_status(db, user_id="user-a")
    assert status["ai"]["used"] == 0
    assert status["ai"]["remaining"] == AI_DAILY_USER_LIMIT
    assert status["debug"]["used"] == 0
    assert status["debug"]["remaining"] == DEBUG_DAILY_USER_LIMIT


def test_release_without_reservation_stays_at_zero(db):
    release_operation(db, user_id="user-a", include_debug=True)
    status = usage_status(db, user_id="user-a")
    assert status["ai"]["used"] == 0
    assert status["debug"]["used"] == 0


def test_double_release_cannot_drive_counter_negative(db):
    reserve_operation(db, user_id="user-a")
    release_operation(db, user_id="user-a")
    release_operation(db, user_id="user-a")
    assert usage_status(db, user_id="user-a")["ai"]["used"] == 0


def test_refunded_slot_is_reusable(db):
    for _ in range(AI_DAILY_USER_LIMIT):
        reserve_operation(db, user_id="user-a")
    with pytest.raises(UsageLimitExceeded):
        reserve_operation(db, user_id="user-a")
    release_operation(db, user_id="user-a")
    reserve_operation(db, user_id="user-a")
    assert usage_status(db, user_id="user-a")["ai"]["remaining"] == 0


def test_release_without_debug_flag_leaves_debug_counter(db):
    reserve_operation(db, user_id="user-a", include_debug=True)
    release_operation(db, user_id="user-a")
    status = usage_status(db, user_id="user-a")
    assert status["ai"]["used"] == 0
    assert status["debug"]["used"] == 1
