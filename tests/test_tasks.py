from unittest.mock import Mock

import pytest
from celery.exceptions import Retry
from pymongo.errors import AutoReconnect, OperationFailure

from credit_service.tasks import apply_credit


@pytest.fixture
def repository(monkeypatch):
    repo = Mock()
    monkeypatch.setattr("credit_service.tasks.CreditRepository", lambda _: repo)
    return repo


def test_worker_closes_its_connection_after_completion(repository):
    apply_credit.run("credit-id")
    repository.finish.assert_called_once_with("credit-id")
    repository.close.assert_called_once()


def test_worker_retries_connection_outage(repository, monkeypatch):
    repository.finish.side_effect = AutoReconnect("temporary network outage")
    retry = Mock(side_effect=Retry())
    monkeypatch.setattr(apply_credit, "retry", retry)
    apply_credit.push_request(retries=2)
    try:
        with pytest.raises(Retry):
            apply_credit.run("credit-id")
    finally:
        apply_credit.pop_request()
    assert 1 <= retry.call_args.kwargs["countdown"] <= 8
    assert repository.finish.call_count == 1
    repository.close.assert_called_once()


def test_worker_records_non_transient_database_failure(repository):
    repository.finish.side_effect = [OperationFailure("invalid schema", code=14), True]
    apply_credit.run("credit-id")
    assert repository.finish.call_args_list[1].kwargs == {"error": "database_operation_failed"}
    repository.close.assert_called_once()


def test_worker_retries_transient_transaction_error(repository, monkeypatch):
    repository.finish.side_effect = OperationFailure(
        "write conflict", code=112, details={"errorLabels": ["TransientTransactionError"]}
    )
    monkeypatch.setattr(apply_credit, "retry", Mock(side_effect=Retry()))
    with pytest.raises(Retry):
        apply_credit.run("credit-id")
    assert repository.finish.call_count == 1
    repository.close.assert_called_once()
