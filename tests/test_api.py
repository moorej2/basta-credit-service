import pytest
from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect

from credit_service.api import create_app

pytestmark = pytest.mark.integration


@pytest.fixture
def client(repo, monkeypatch):
    monkeypatch.setattr("credit_service.api.dispatch_one", lambda *args: False)
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

    monkeypatch.setattr("credit_service.api.dispatch_one", broker_down)
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
