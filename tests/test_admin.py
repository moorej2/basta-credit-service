import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest

from credit_service import admin

pytestmark = pytest.mark.integration


@pytest.fixture
def run_admin(repo, monkeypatch):
    monkeypatch.setattr(admin, "CreditRepository", lambda _: repo)
    monkeypatch.setattr(repo, "close", Mock())

    def run(*args):
        monkeypatch.setattr("sys.argv", ["credits-admin", *args])
        admin.main()

    return run


def test_admin_reports_pending_age_and_failures(repo, run_admin, capsys):
    credit = repo.accept("user", Decimal("1"), "first")
    repo.credits.update_one(
        {"_id": credit["_id"]},
        {
            "$set": {
                "created_at": datetime.now(UTC) - timedelta(seconds=60),
            }
        },
    )
    failed = repo.accept("user", Decimal("1"), "second")
    repo.finish(failed["_id"], error="bad data")
    run_admin("status")
    status = json.loads(capsys.readouterr().out)
    assert status["processing"] == status["failed"] == 1
    assert status["oldest_pending_seconds"] >= 60
    repo.close.assert_called_once()


def test_admin_requeues_failed_credit(repo, run_admin, capsys):
    credit = repo.accept("user", Decimal("1"), "key")
    repo.finish(credit["_id"], error="bad data")
    run_admin("requeue", credit["_id"])
    assert "requeued" in capsys.readouterr().out
    assert repo.get_credit(credit["_id"])["status"] == "processing"
    assert repo.get_user("user")["pending_credits"] == 1
    repo.close.assert_called_once()


def test_admin_refuses_reopening_a_processed_credit(repo, run_admin):
    credit = repo.accept("user", Decimal("1"), "key")
    repo.finish(credit["_id"])
    with pytest.raises(SystemExit) as exc:
        run_admin("requeue", credit["_id"])
    assert exc.value.code == 1
    assert repo.get_user("user")["balance"].to_decimal() == Decimal("1")
    repo.close.assert_called_once()


def test_admin_requires_id_and_closes_connection(run_admin, repo):
    with pytest.raises(SystemExit) as exc:
        run_admin("requeue")
    assert exc.value.code == 2
    repo.close.assert_called_once()
