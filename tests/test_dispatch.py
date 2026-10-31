from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from credit_service.dispatch import dispatch_one

pytestmark = pytest.mark.integration


def test_publish_failure_remains_recoverable(repo, monkeypatch):
    credit = repo.accept("u", Decimal("1"), "key")

    def unavailable(*args):
        raise ConnectionError("injected broker failure")

    monkeypatch.setattr("credit_service.dispatch.publish", unavailable)
    with pytest.raises(ConnectionError):
        dispatch_one(repo)
    repo.credits.update_one(
        {"_id": credit["_id"]},
        {
            "$set": {
                "next_dispatch_at": datetime.now(UTC) - timedelta(seconds=1),
            }
        },
    )
    published = []
    monkeypatch.setattr("credit_service.dispatch.publish", published.append)
    assert dispatch_one(repo)
    assert published == [credit["_id"]]
