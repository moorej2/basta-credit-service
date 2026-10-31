from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from bson.decimal128 import Decimal128

from credit_service.repository import IdempotencyConflict

pytestmark = pytest.mark.integration


def accept(repo, amount="50.00", user="u_123", key=None):
    return repo.accept(user, Decimal(amount), key or str(uuid4()))


def test_creates_missing_balance_and_applies_once(repo):
    repo.users.insert_one({"user_id": "u_123"})
    credit = accept(repo)
    assert repo.get_user("u_123")["status"] == "processing"
    assert "balance" not in repo.get_user("u_123")
    assert repo.finish(credit["_id"]) is True
    assert repo.finish(credit["_id"]) is False
    user = repo.get_user("u_123")
    assert user["balance"].to_decimal() == Decimal("50.00")
    assert user["status"] == "processed"
    assert user["pending_credits"] == 0


def test_existing_balance_is_incremented(repo):
    repo.users.insert_one({"user_id": "u_123", "balance": Decimal128("100.00")})
    credit = accept(repo)
    repo.finish(credit["_id"])
    assert repo.get_user("u_123")["balance"].to_decimal() == Decimal("150.00")


def test_idempotent_request_and_conflict(repo):
    first = accept(repo, key="same-request")
    assert accept(repo, amount="50", key="same-request")["_id"] == first["_id"]
    with pytest.raises(IdempotencyConflict):
        accept(repo, amount="51", key="same-request")
    with pytest.raises(IdempotencyConflict):
        accept(repo, user="another", key="same-request")
    assert repo.credits.count_documents({}) == 1
    assert repo.get_user("u_123")["pending_credits"] == 1


def test_concurrent_same_request_is_accepted_once(repo):
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(lambda _: accept(repo, key="same"), range(8)))
    assert len({record["_id"] for record in records}) == 1
    assert repo.get_user("u_123")["pending_credits"] == 1


def test_concurrent_new_user_acceptance_and_processing(repo):
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(lambda _: accept(repo, amount="0.10"), range(16)))
        list(pool.map(lambda record: repo.finish(record["_id"]), records))
    user = repo.get_user("u_123")
    assert user["balance"].to_decimal() == Decimal("1.60")
    assert user["pending_credits"] == 0
    assert user["status"] == "processed"


def test_concurrent_duplicate_delivery_changes_balance_once(repo):
    record = accept(repo)
    with ThreadPoolExecutor(max_workers=8) as pool:
        applied = list(pool.map(lambda _: repo.finish(record["_id"]), range(8)))
    assert applied.count(True) == 1
    assert repo.get_user("u_123")["balance"].to_decimal() == Decimal("50.00")


def test_status_stays_processing_until_all_work_finishes(repo):
    first, second = accept(repo), accept(repo)
    repo.finish(first["_id"])
    assert repo.get_user("u_123")["status"] == "processing"
    repo.finish(second["_id"])
    assert repo.get_user("u_123")["status"] == "processed"


def test_transaction_rolls_back_balance_when_completion_write_fails(repo, monkeypatch):
    record = accept(repo)
    original = repo.credits.update_one

    def fail(*args, **kwargs):
        raise RuntimeError("injected failure after balance update")

    monkeypatch.setattr(repo.credits, "update_one", fail)
    with pytest.raises(RuntimeError, match="injected failure"):
        repo.finish(record["_id"])
    assert "balance" not in repo.get_user("u_123")
    assert repo.get_user("u_123")["pending_credits"] == 1
    assert repo.get_credit(record["_id"])["status"] == "processing"
    monkeypatch.setattr(repo.credits, "update_one", original)
    assert repo.finish(record["_id"])


def test_commit_then_redelivery_is_harmless(repo):
    record = accept(repo)
    repo.finish(record["_id"])
    # Simulate loss of acknowledgment after the transaction committed.
    assert not repo.finish(record["_id"])
    assert repo.get_user("u_123")["balance"].to_decimal() == Decimal("50.00")


def test_dispatch_lease_expires_and_terminal_records_are_not_republished(repo):
    record = accept(repo)
    assert repo.claim_dispatch(record["_id"])
    assert repo.claim_dispatch(record["_id"]) is None
    repo.credits.update_one(
        {"_id": record["_id"]},
        {
            "$set": {
                "next_dispatch_at": datetime.now(UTC) - timedelta(seconds=1),
            }
        },
    )
    assert repo.claim_dispatch(record["_id"])["dispatch_count"] == 2
    repo.finish(record["_id"])
    assert repo.claim_dispatch(record["_id"]) is None


def test_failed_credit_recovery_preserves_counters_and_money(repo):
    first, second = accept(repo), accept(repo)
    repo.finish(first["_id"], error="database_operation_failed")
    assert repo.get_user("u_123")["status"] == "processing"
    repo.finish(second["_id"])
    assert repo.get_user("u_123")["status"] == "failed"
    assert repo.requeue_failed(first["_id"])
    assert not repo.requeue_failed(first["_id"])
    repo.finish(first["_id"])
    user = repo.get_user("u_123")
    assert user["status"] == "processed"
    assert user["failed_credits"] == user["pending_credits"] == 0
    assert user["balance"].to_decimal() == Decimal("100.00")
