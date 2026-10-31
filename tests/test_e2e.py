import os
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.e2e


def wait_processed(client, credit_id):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        response = client.get(f"/credits/{credit_id}")
        response.raise_for_status()
        record = response.json()
        assert record["status"] != "failed", record
        if record["status"] == "processed":
            return
        time.sleep(0.2)
    pytest.fail(f"Credit {credit_id} did not complete within 45 seconds")


def test_real_api_broker_worker_and_database():
    url = os.getenv("CREDIT_API_URL")
    if not url:
        pytest.skip("Set CREDIT_API_URL or run Docker Compose test service")
    user = f"e2e_{uuid4().hex}"
    with httpx.Client(base_url=url, timeout=15) as client:

        def submit(i):
            result = client.post(
                "/credits",
                json={"user_id": user, "amount": "0.10"},
                headers={"Idempotency-Key": f"{user}:{i}"},
            )
            assert result.status_code == 202, result.text
            return result.json()["task_id"]

        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(submit, range(12)))
        assert submit(0) == ids[0]
        for credit_id in ids:
            wait_processed(client, credit_id)
        result = client.get(f"/users/{user}").json()
        assert result["balance"] == "1.20", result
        assert result["pending_credits"] == 0
        assert result["status"] == "processed"
