from unittest.mock import Mock

import pytest
from celery.exceptions import Reject, Retry
from kombu.exceptions import OperationalError
from pymongo.errors import AutoReconnect, OperationFailure, WTimeoutError

from credit_service import tasks
from credit_service.tasks import apply_credit


@pytest.fixture
def repository(monkeypatch):
    repo = Mock()
    monkeypatch.setattr(tasks, "_worker_repository", None)
    monkeypatch.setattr(tasks, "CreditRepository", Mock(return_value=repo))
    return repo


def test_client_is_reused_between_tasks_and_closed_at_worker_shutdown(repository):
    apply_credit.run("first")
    apply_credit.run("second")
    assert tasks.CreditRepository.call_count == 1
    repository.close.assert_not_called()
    tasks.close_worker_repository()
    repository.close.assert_called_once()
    assert tasks._worker_repository is None


def test_forked_child_gets_its_own_client_without_touching_parent_client(repository, monkeypatch):
    parent = Mock()
    monkeypatch.setattr(tasks, "_worker_repository", (-1, parent))
    assert tasks.get_worker_repository() is repository
    parent.close.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        AutoReconnect("network"),
        WTimeoutError("write concern timed out", code=64),
        OperationFailure("primary stepping down", code=91),
        OperationFailure(
            "unknown commit", details={"errorLabels": ["UnknownTransactionCommitResult"]}
        ),
        OperationFailure("conflict", details={"errorLabels": ["TransientTransactionError"]}),
    ],
)
def test_infrastructure_errors_do_not_consume_failure_budget(repository, error):
    repository.finish.side_effect = error
    apply_credit.run("credit")
    repository.defer.assert_called_once_with("credit", "database_unavailable", count_failure=False)


@pytest.mark.parametrize("error", [OperationFailure("bad data", code=14), RuntimeError("bug")])
def test_actual_errors_use_durable_failure_budget(repository, error):
    repository.finish.side_effect = error
    apply_credit.run("credit")
    repository.defer.assert_called_once_with("credit", "task_execution_failed", count_failure=True)


def test_mongo_outage_falls_back_to_broker_retry(repository, monkeypatch):
    repository.finish.side_effect = AutoReconnect("network")
    repository.defer.side_effect = AutoReconnect("network")
    retry = Mock(side_effect=Retry())
    monkeypatch.setattr(apply_credit, "retry", retry)
    apply_credit.push_request(retries=2)
    try:
        with pytest.raises(Retry):
            apply_credit.run("credit")
    finally:
        apply_credit.pop_request()
    assert 1 <= retry.call_args.kwargs["countdown"] <= 8


def test_both_stores_down_requeues_original(repository, monkeypatch):
    repository.finish.side_effect = AutoReconnect("database down")
    repository.defer.side_effect = AutoReconnect("database down")
    signature = Mock()
    signature.apply_async.side_effect = OperationalError("broker down")
    monkeypatch.setattr(apply_credit, "signature_from_request", Mock(return_value=signature))
    apply_credit.push_request(retries=0, called_directly=False, is_eager=False)
    try:
        with pytest.raises(Reject) as result:
            apply_credit.run("credit")
    finally:
        apply_credit.pop_request()
    assert result.value.requeue is True
