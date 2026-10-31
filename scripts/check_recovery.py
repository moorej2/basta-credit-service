"""Exercise real worker/broker recovery on this project's local Compose stack.

Temporarily stops this project's worker and RabbitMQ. Restarts both in finally.
Uses Python's standard library; run from the host with Docker available.
"""

import json
import os
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = f"http://localhost:{os.environ.get('CREDIT_API_PORT', '8000')}"


def compose(*args):
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True)


def request(path, data=None, key=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Idempotency-Key"] = key
    req = Request(
        BASE_URL + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers=headers,
    )
    with urlopen(req, timeout=20) as response:
        return response.status, json.load(response)


def wait_processed(credit_id):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        _, credit = request(f"/credits/{credit_id}")
        if credit["status"] == "processed":
            return
        if credit["status"] == "failed":
            raise AssertionError(credit)
        time.sleep(0.5)
    raise AssertionError(f"Credit did not recover within 120 seconds: {credit_id}")


def main():
    user = f"recovery_{uuid4().hex}"
    payload = {"user_id": user, "amount": "1.25"}
    worker_key, broker_key = f"{user}:worker", f"{user}:broker"
    try:
        compose("stop", "worker")
        status, first = request("/credits", payload, worker_key)
        assert status == 202 and first["status"] == "processing", first
        _, waiting = request(f"/credits/{first['task_id']}")
        assert waiting["status"] == "processing", waiting
        print("PASS: 202 and processing while worker is stopped", flush=True)
        compose("start", "worker")
        wait_processed(first["task_id"])
        _, replay = request("/credits", payload, worker_key)
        assert replay["task_id"] == first["task_id"]
        _, account = request(f"/users/{user}")
        assert Decimal(account["balance"]) == Decimal("1.25"), account
        print("PASS: worker restart completed credit; request replay did not add money", flush=True)

        compose("stop", "rabbitmq")
        start = time.monotonic()
        status, second = request("/credits", payload, broker_key)
        elapsed = time.monotonic() - start
        assert status == 202 and second["status"] == "processing", second
        print(f"PASS: broker down; credit durably accepted in {elapsed:.2f}s", flush=True)
        compose("start", "rabbitmq")
        wait_processed(second["task_id"])
        _, account = request(f"/users/{user}")
        assert Decimal(account["balance"]) == Decimal("2.50"), account
        assert account["pending_credits"] == 0 and account["status"] == "processed", account
        print("PASS: broker recovery completed credit; exact total 2.50, zero pending", flush=True)
        print(
            json.dumps(
                {
                    "user_id": user,
                    "worker_credit": first["task_id"],
                    "broker_credit": second["task_id"],
                }
            ),
            flush=True,
        )
    finally:
        compose("start", "rabbitmq", "worker")


if __name__ == "__main__":
    main()
