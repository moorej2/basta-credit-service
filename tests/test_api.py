import pytest
from bson.decimal128 import Decimal128
from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect

from credit_service.api import create_app

pytestmark = pytest.mark.integration


@pytest.fixture
def client(repo):
    with TestClient(create_app(repo)) as test_client:
        yield test_client


def test_api_acceptance_polling_and_user_balance(client, repo):
    response = client.post("/credits", json={"user_id": "u_123", "amount": "50.00"})
    assert response.status_code == 202
    credit_id = response.json()["task_id"]
    assert response.headers["location"] == f"/credits/{credit_id}"
    assert client.get(response.headers["location"]).json()["status"] == "processing"
    repo.finish(credit_id)
    assert client.get(response.headers["location"]).json()["status"] == "processed"
    assert client.get("/users/u_123").json()["balance"] == "50.00"


def test_api_repeated_key_and_conflict(client):
    payload = {"user_id": "u_123", "amount": "10.00"}
    headers = {"Idempotency-Key": "order-1"}
    first = client.post("/credits", json=payload, headers=headers)
    second = client.post("/credits", json=payload, headers=headers)
    assert first.json()["task_id"] == second.json()["task_id"]
    payload["amount"] = "11.00"
    assert client.post("/credits", json=payload, headers=headers).status_code == 409


def test_api_keeps_accepted_work_when_publish_fails(client, repo, monkeypatch):
    def broker_down(*args):
        raise ConnectionError("broker unavailable")

    monkeypatch.setattr("credit_service.dispatch.publish", broker_down)
    response = client.post("/credits", json={"user_id": "u_123", "amount": "1.00"})
    assert response.status_code == 202
    assert repo.get_credit(response.json()["task_id"])["status"] == "processing"


def test_api_rejects_bad_input_and_missing_records(client):
    for amount in ["0", "-1", "1.001", "NaN"]:
        assert (
            client.post("/credits", json={"user_id": "u_123", "amount": amount}).status_code == 422
        )
    assert client.get("/credits/missing").status_code == 404
    assert client.get("/users/missing").status_code == 404
    assert (
        client.post(
            "/credits", json={"user_id": "u", "amount": 1}, headers={"Idempotency-Key": " "}
        ).status_code
        == 422
    )


def test_database_unavailable_does_not_claim_acceptance(client, repo, monkeypatch):
    def unavailable(*args):
        raise AutoReconnect("database unavailable")

    monkeypatch.setattr(repo, "accept", unavailable)
    assert client.post("/credits", json={"user_id": "u", "amount": 1}).status_code == 503


@pytest.mark.parametrize("balance", [150.0, 150, Decimal128("150.00")])
def test_spec_shaped_seeded_users_can_be_read(client, repo, balance):
    repo.users.insert_one({"user_id": "seeded", "balance": balance, "status": "processed"})
    response = client.get("/users/seeded")
    assert response.status_code == 200
    assert response.json() == {
        "user_id": "seeded",
        "balance": "150.00",
        "status": "processed",
        "pending_credits": 0,
        "failed_credits": 0,
    }


def test_seeded_user_without_balance_reads_as_zero(client, repo):
    repo.users.insert_one({"user_id": "seeded", "status": "processed"})
    assert client.get("/users/seeded").json()["balance"] == "0.00"


def test_readiness_database_error_does_not_advise_request_idempotency(client, repo, monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(repo, "client", Mock())
    repo.client.admin.command.side_effect = AutoReconnect("down")
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}
    assert client.get("/health/live").status_code == 200
