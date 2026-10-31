# Async credit processing service

A small FastAPI service that accepts a credit, returns `202 Accepted`, and uses
RabbitMQ and a Celery worker to update a MongoDB balance asynchronously.
MongoDB transactions keep the credit outcome and balance consistent. Durable
pending credit records recover interrupted publication and processing.

The [two-page design document](docs/design.pdf) explains the architecture,
implementation decisions, failure recovery, and testing strategy.
See [verification evidence](docs/VERIFICATION.md) for the checks performed and
their limitations.

## Run

Prerequisites: Docker Engine with Docker Compose v2, internet for the initial
image build, and about 2 GB of available memory. No host Python is needed.

```bash
docker compose up --build -d --wait
curl http://localhost:8000/health/ready
```

- API and interactive OpenAPI documentation: http://localhost:8000/docs
- RabbitMQ management: http://localhost:15672 (`credits` / `credits`)
- Only the API and broker management ports are published, both on loopback.
- MongoDB runs as a single-node replica set because transactions require it.
- Data survives container restarts in this project's named volumes.
- Copy `.env.example` to `.env` only if changing defaults, such as the API port.

Submit a credit:

```bash
curl -i http://localhost:8000/credits \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: example-credit-001' \
  -d '{"user_id":"u_123","amount":"50.00"}'
```

The response contains a `task_id` and a `Location` header. Poll using that ID:

```bash
curl http://localhost:8000/credits/REPLACE_WITH_TASK_ID
curl http://localhost:8000/users/u_123
```

The user response eventually contains `"balance":"50.00"` and
`"status":"processed"`. Balances and amounts are JSON strings to preserve exact
decimal values. A missing MongoDB `balance` field is created by the worker's
`$inc`. Repeating the same request with the same key returns the original credit
without adding money again. Use a new key to request another credit.

## API contract

| Endpoint | Behavior |
| --- | --- |
| `POST /credits` | Validates and durably accepts a credit; returns 202 and the stable credit/task ID |
| `GET /credits/{task_id}` | Returns `processing`, `processed`, or `failed`; 404 if absent |
| `GET /users/{user_id}` | Returns balance, aggregate status, pending count, failed count |
| `GET /health/live` | Process liveness |
| `GET /health/ready` | MongoDB connectivity; broker outages do not block durable acceptance |

Input rules: user IDs are 1-128 ASCII letters, digits, underscores, or hyphens;
amounts are finite, positive, at most 1,000,000,000, with at most two fractional
decimal places. Unknown fields are rejected. Invalid input returns 422.
The currency is fixed by assumption; there is no exchange or rounding policy.
Prefer a decimal string such as `"0.10"` over a JSON floating-point number.

`Idempotency-Key` is optional to preserve the exercise's minimal request contract.
It permits 1-128 ASCII letters, digits, `_`, `.`, `:`, and `-`. Keys are global to
this single-tenant service and retained with credit records. A repeated key with
a different user or amount returns 409. Without a key, every call is a new credit:
safe recovery from an ambiguous HTTP outcome requires a caller-supplied key.
A replay returns the original ID and current status, still with HTTP 202.

A 202 means the operation is committed to MongoDB, not that money has been
applied. Publication is attempted before responding; if the broker is down,
acceptance still succeeds and the dispatcher retries after the lease expires.
Database failures return 503; a client must reuse its key because a commit may
have succeeded before the connection was interrupted. Requests wait for database
acceptance and an initial publication attempt, never for worker completion.
PyMongo's transaction retries may extend request time during contention/outages;
this demo has connection/socket limits, not a hard end-to-end HTTP deadline.

## Verify

Run the complete suite against real MongoDB, RabbitMQ, the API, and a prefork worker:

```bash
docker compose --profile test run --build --rm test
```

This executes validation, API, transactional integration, concurrency, failure
injection, dispatch recovery, and end-to-end tests. Database integration tests use
unique disposable database names and do not drop the application's database.
The end-to-end test adds an isolated `e2e_...` user to the running application.

Run only transactional/API integration tests:

```bash
docker compose --profile test run --rm test pytest -q -m integration
```

For host-based development, use Python 3.11+ (3.12 used for this submission):

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -q -m 'not integration and not e2e'
```

The default Compose network does not expose MongoDB/AMQP to host processes.
Use the test container for integration tests. Running bare `pytest` on the host
without infrastructure variables skips integration/E2E tests; that is **not** the
full verification result. CI runs both local checks and the full Docker suite.

`requirements.lock` pins and hashes runtime dependencies; `requirements-dev.lock`
does the same with development extras. To intentionally refresh them, install
`pip-tools==7.6.2` in the virtual environment and run:

```bash
.venv/bin/pip-compile --generate-hashes -o requirements.lock pyproject.toml
.venv/bin/pip-compile --extra=dev --generate-hashes -o requirements-dev.lock pyproject.toml
```

## Architecture and code map

1. The API transaction creates a credit and increments the user's pending count.
2. The API attempts publication with the credit ID. The dispatcher also polls
   pending credits and atomically claims expiring dispatch leases.
3. RabbitMQ delivers a persistent JSON task to Celery; publisher confirms are enabled.
4. The worker transaction checks the credit, increments `balance`, decrements the
   pending count, computes aggregate status, and marks the credit processed.
5. Duplicate workers see a completed credit after transaction retry and do nothing.

| File | Responsibility |
| --- | --- |
| `credit_service/api.py` | HTTP contract and acceptance/polling endpoints |
| `credit_service/models.py` | Validation and decimal-safe response models |
| `credit_service/repository.py` | Transactions, uniqueness, status, dispatch leases |
| `credit_service/celery_app.py` | Queue, acknowledgment, serialization configuration |
| `credit_service/tasks.py` | Worker execution and transient failure handling |
| `credit_service/dispatch.py` | Initial publication and durable periodic recovery |
| `credit_service/admin.py` | Queue age/count inspection and explicit failed-credit recovery |

The stable task ID is a correlation ID, **not** a Celery deduplication mechanism.
MongoDB enforces idempotency. No Celery result backend is needed because operation
status is already stored durably in MongoDB.

## Operations and recovery

```bash
docker compose ps
docker compose logs --tail=100 api worker dispatcher
docker compose exec api python -m credit_service.admin status
```

Stop the worker, submit a new credit, verify `processing`, then start it again:

```bash
docker compose stop worker
# POST a credit with a NEW idempotency key, then poll its Location.
docker compose start worker
```

To demonstrate broker recovery, stop `rabbitmq`, submit a new credit (202), then
start `rabbitmq`. Within the 30-second lease plus polling/processing time, the
dispatcher republishes it. Keep the API/dispatcher running during this exercise.
Worker connection recovery can add broker reconnect delay. Publication retries
and processing retries are separate mechanisms.

For a repeatable real-outage check using the host Python standard library:

```bash
python3 scripts/check_recovery.py
```

This temporarily stops/restarts this project's worker and broker, checks durable
acceptance and the exact recovered balance, then leaves both services running.
Use it only when you are ready to interrupt this local demo stack. If using a
non-default port, export `CREDIT_API_PORT` before running the script.

Transient connection and transaction failures retry with delay; connection
failures use jitter and a capped exponential delay. They are not automatically
declared permanent simply because an outage lasts a long time. A non-transient
MongoDB operation error is recorded as `failed` when MongoDB can persist that
outcome. Unexpected task errors remain pending for recovery and produce error logs.
Alert on old pending credits, failed credits, and repeated task/publish errors.

After diagnosing and fixing a failed operation's cause:

```bash
docker compose exec api python -m credit_service.admin requeue REPLACE_WITH_TASK_ID
```

Recovery adjusts the failed/pending counters transactionally and preserves the
original credit ID. It never reopens an already processed credit.

Aggregate user status is `processing` while any credits are pending, otherwise
`failed` while unresolved failed credits exist, otherwise `processed`. A failed
credit never increments the balance. Individual outcomes remain queryable.

Stop without deleting data:

```bash
docker compose down
```

For a deliberate clean slate, `docker compose down -v` deletes **this project's**
MongoDB and RabbitMQ volumes. Do not use it when you need the demo data.

## Scope and trade-offs

The replayable credit record is a compact outbox/work queue. It republishes pending
work every lease period until a terminal outcome, including tasks that are merely
waiting in RabbitMQ. This deliberately trades extra duplicate messages for recovery
from publish/crash uncertainty. At larger scale, add bounded admission, dispatch
backoff, queue-age alarms, and separate acknowledged-outbox/reconciliation policies.

Single-node MongoDB and RabbitMQ are development topology, not high availability.
Container image tags track patch updates; dependency locks do not pin base image
digests. Production needs authenticated users, tenant-scoped keys, authorization,
rate limits, TLS, managed secrets, least-privilege database/broker identities,
backups with restore tests, operational dashboards, deployment controls, and an
audited monetary ledger/reconciliation policy. No real payment rail is integrated.
The service is intended for local review with the included demo credentials.
