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


def expire_dispatch(repo, credit_id):
    repo.credits.update_one(
        {"_id": credit_id},
        {
            "$set": {
                "next_dispatch_at": datetime.now(UTC) - timedelta(seconds=1),
            }
        },
    )


def test_publication_backoff_caps_without_failing_a_waiting_credit(repo):
    credit = accept(repo)
    for expected in [30, 60, 120, 240, 300, 300]:
        record = repo.claim_dispatch(credit["_id"])
        delay = (record["next_dispatch_at"] - datetime.now(UTC)).total_seconds()
        assert expected - 2 <= delay <= expected
        assert record["status"] == "processing"
        assert record["failure_count"] == 0
        expire_dispatch(repo, credit["_id"])
    assert repo.get_user("u_123")["pending_credits"] == 1


def test_confirmed_message_is_not_republished_while_waiting_in_queue(repo):
    credit = accept(repo)
    claim = repo.claim_dispatch(credit["_id"])
    assert repo.mark_published(credit["_id"], claim["dispatch_token"])
    expire_dispatch(repo, credit["_id"])
    assert repo.claim_dispatch(credit["_id"]) is None
    assert repo.get_credit(credit["_id"])["status"] == "processing"


def test_stale_publisher_cannot_overwrite_a_new_lease(repo):
    credit = accept(repo)
    old = repo.claim_dispatch(credit["_id"])
    expire_dispatch(repo, credit["_id"])
    new = repo.claim_dispatch(credit["_id"])
    assert not repo.mark_published(credit["_id"], old["dispatch_token"])
    assert repo.mark_published(credit["_id"], new["dispatch_token"])


def test_fast_worker_retry_is_not_hidden_by_late_publication_confirmation(repo):
    credit = accept(repo)
    claim = repo.claim_dispatch(credit["_id"])
    assert repo.defer(credit["_id"], "injected error", count_failure=True) == "retry"
    assert not repo.mark_published(credit["_id"], claim["dispatch_token"])
    assert repo.get_credit(credit["_id"])["dispatch_state"] == "pending"
    expire_dispatch(repo, credit["_id"])
    assert repo.claim_dispatch(credit["_id"])


def test_repeated_execution_errors_park_credit_and_requeue_resets_budget(repo):
    credit = accept(repo)
    for i in range(repo.settings.task_max_failures):
        outcome = repo.defer(credit["_id"], "bug", count_failure=True)
        assert outcome == ("failed" if i == repo.settings.task_max_failures - 1 else "retry")
    user = repo.get_user("u_123")
    assert user["status"] == "failed"
    assert user["pending_credits"] == 0 and user["failed_credits"] == 1
    assert "balance" not in user
    assert repo.claim_dispatch(credit["_id"]) is None
    assert repo.defer(credit["_id"], "bug", count_failure=True) == "terminal"
    assert repo.requeue_failed(credit["_id"])
    assert repo.get_credit(credit["_id"])["failure_count"] == 0
    repo.finish(credit["_id"])
    assert repo.get_user("u_123")["balance"].to_decimal() == Decimal("50.00")


def test_infrastructure_retries_back_off_without_consuming_failure_budget(repo):
    credit = accept(repo)
    for expected in [2, 4, 8, 16, 32, 60, 60]:
        assert repo.defer(credit["_id"], "database_unavailable", count_failure=False) == "retry"
        record = repo.get_credit(credit["_id"])
        delay = (record["next_dispatch_at"] - datetime.now(UTC)).total_seconds()
        assert expected - 2 <= delay <= expected
        assert record["failure_count"] == 0
    assert repo.get_user("u_123")["pending_credits"] == 1


def test_error_after_successful_commit_cannot_park_or_reopen_credit(repo):
    credit = accept(repo)
    repo.finish(credit["_id"])
    assert repo.defer(credit["_id"], "uncertain outcome", count_failure=True) == "terminal"
    assert repo.get_credit(credit["_id"])["status"] == "processed"
    assert repo.get_user("u_123")["balance"].to_decimal() == Decimal("50.00")


def test_failure_transition_racing_success_keeps_money_and_status_consistent(repo):
    repo.settings.task_max_failures = 1
    credit = accept(repo)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(repo.finish, credit["_id"])
        second = pool.submit(repo.defer, credit["_id"], "bug", count_failure=True)
        first.result()
        second.result()
    record, user = repo.get_credit(credit["_id"]), repo.get_user("u_123")
    assert user["pending_credits"] == 0
    if record["status"] == "processed":
        assert user["balance"].to_decimal() == Decimal("50.00")
        assert user["failed_credits"] == 0
    else:
        assert record["status"] == "failed" and user["failed_credits"] == 1
        assert "balance" not in user


@pytest.mark.parametrize("balance", [100, 100.0])
def test_credit_applies_to_legacy_numeric_balance(repo, balance):
    repo.users.insert_one({"user_id": "u_123", "balance": balance, "status": "processed"})
    credit = accept(repo)
    repo.finish(credit["_id"])
    assert repo.get_user("u_123")["balance"].to_decimal() == Decimal("150.00")


def test_missing_user_cannot_produce_a_false_processed_marker(repo):
    credit = accept(repo)
    repo.users.delete_one({"user_id": "u_123"})
    with pytest.raises(RuntimeError, match="no user"):
        repo.finish(credit["_id"])
    assert repo.get_credit(credit["_id"])["status"] == "processing"


def test_real_worker_handler_parks_bad_balance_after_actual_failures(repo, monkeypatch):
    from credit_service import tasks

    credit = accept(repo)
    repo.users.update_one({"user_id": "u_123"}, {"$set": {"balance": "corrupt"}})
    monkeypatch.setattr(tasks, "get_worker_repository", lambda: repo)
    for _ in range(repo.settings.task_max_failures):
        tasks.apply_credit.run(credit["_id"])
    record = repo.get_credit(credit["_id"])
    assert record["status"] == "failed"
    assert record["failure_count"] == repo.settings.task_max_failures
    assert repo.get_user("u_123")["balance"] == "corrupt"
    assert repo.get_user("u_123")["pending_credits"] == 0


def test_handler_preserves_money_after_ambiguous_commit(repo, monkeypatch):
    from pymongo.errors import OperationFailure

    from credit_service import tasks

    credit = accept(repo)
    finish = repo.finish

    def committed_then_lost_reply(credit_id):
        finish(credit_id)
        raise OperationFailure(
            "lost commit reply",
            details={
                "errorLabels": ["UnknownTransactionCommitResult"],
            },
        )

    monkeypatch.setattr(tasks, "get_worker_repository", lambda: repo)
    monkeypatch.setattr(repo, "finish", committed_then_lost_reply)
    tasks.apply_credit.run(credit["_id"])
    assert repo.get_credit(credit["_id"])["status"] == "processed"
    assert repo.get_credit(credit["_id"])["failure_count"] == 0
    assert repo.get_user("u_123")["balance"].to_decimal() == Decimal("50.00")
