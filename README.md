# Async credit processing service

[![CI](https://github.com/moorej2/basta-credit-service/actions/workflows/ci.yml/badge.svg)](https://github.com/moorej2/basta-credit-service/actions/workflows/ci.yml)

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

Existing spec-shaped user documents with BSON double/integer balances and missing
counters can be read and credited; missing counters default to zero. Legacy
doubles are converted for display, which cannot recover precision already lost
when the double was stored. New credit amounts use Decimal128 throughout.

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

A 202 means the operation and its dispatch intent are committed to MongoDB, not
that money has been applied. Only the dispatcher contacts RabbitMQ; an API request
never waits for broker connection or publisher confirmation. During a broker
outage, credits remain durably pending until publication succeeds.
Database failures return 503; a client must reuse its key because a commit may
have succeeded before the connection was interrupted. Requests wait for database
acceptance, never for broker publication or worker completion.
PyMongo's transaction retries may extend request time during contention/outages;
this demo has connection/socket limits, not a hard end-to-end HTTP deadline.

## Retry and timeout settings

Compose accepts these values through `.env` or exported environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CREDIT_DISPATCH_LEASE_SECONDS` | 30 | Initial delay before another unconfirmed publication attempt |
| `CREDIT_DISPATCH_BACKOFF_MAX_SECONDS` | 300 | Maximum publication retry delay |
| `CREDIT_TASK_RETRY_BASE_SECONDS` | 2 | Initial stored execution retry delay |
| `CREDIT_TASK_RETRY_MAX_SECONDS` | 60 | Maximum execution retry delay |
| `CREDIT_TASK_MAX_FAILURES` | 5 | Actual non-infrastructure failures before operator review |
| `CREDIT_MONGO_TIMEOUT_MS` | 3000 | Server selection and connection timeout |
| `CREDIT_MONGO_SOCKET_TIMEOUT_MS` | 10000 | Socket operation timeout |

These are individual operation limits, not a hard HTTP deadline. Database driver
transaction retries can extend request latency.

## Verify

Run the complete suite against real MongoDB, RabbitMQ, the API, and a prefork worker:

```bash
docker compose --profile test run --build --rm test
```

This executes validation, API, transactional integration, concurrency, failure
injection, dispatch recovery, and end-to-end tests. Regression tests also cover
legacy numeric balances, health failures, process-local client reuse, outbox
confirmation races, poison credits, retry classification, operator CLI behavior,
and dispatcher shutdown/recovery. Database integration tests use
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
2. The dispatcher polls the durable dispatch intent, claims an expiring lease,
   and publishes the credit ID. Publisher confirmation marks that intent sent;
   queued credits are not republished simply because they are still processing.
3. RabbitMQ delivers a persistent JSON task to Celery; publisher confirms are enabled.
4. The worker transaction checks the credit, increments `balance`, decrements the
   pending count, computes aggregate status, and marks the credit processed.
5. Duplicate workers see a completed credit after transaction retry and do nothing.
   Each worker process lazily reuses its own MongoDB connection pool.

| File | Responsibility |
| --- | --- |
| `credit_service/api.py` | HTTP contract and acceptance/polling endpoints |
| `credit_service/models.py` | Validation and decimal-safe response models |
| `credit_service/repository.py` | Transactions, uniqueness, status, dispatch leases |
| `credit_service/celery_app.py` | Queue, acknowledgment, serialization configuration |
| `credit_service/tasks.py` | Worker execution and transient failure handling |
| `credit_service/dispatch.py` | Confirmed outbox publication and backoff after interrupted dispatch |
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
start `rabbitmq`. The dispatcher retries failed publication with increasing
leases: 30, 60, 120, 240, then at most 300 seconds by default. A fresh credit
usually gets its first publication attempt within the two-second polling interval.
Keep the API/dispatcher running during this exercise.
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

Task errors persist delayed retry intent in MongoDB, so publication and execution
retries have separate counters. Task retry delays double from 2 to at most 60
seconds. Connection failures, write-concern errors, uncertain commits, and known
transient database codes do not consume the failure budget. They can retry for
as long as infrastructure is unavailable.

Unexpected exceptions and other database operation errors consume the actual
execution-failure budget. After five recorded failures (configurable), the credit
is parked as `failed` and the user's counters are updated in the same transaction.
Dispatch attempts and time waiting in a queue never count toward that limit.
A failure recorded after an ambiguous successful commit sees `processed` and
cannot reopen or reclassify the credit.

If MongoDB cannot persist retry intent, Celery schedules a broker retry with
jitter. If that publication also fails, the original delivery is rejected with
requeue enabled rather than acknowledged. This can cause repeated redelivery
while both dependencies are down; alert and restore infrastructure rather than
expect a terminal business failure. Alert on oldest pending age, failed credits,
and repeated task/publish errors.

After diagnosing and fixing a failed operation's cause:

```bash
docker compose exec api python -m credit_service.admin requeue REPLACE_WITH_TASK_ID
```

Recovery adjusts the failed/pending counters transactionally and preserves the
original credit ID, resets its failure budget, and schedules fresh dispatch. It
never reopens an already processed credit.

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

The credit record doubles as a transactional outbox: saving a request also saves
the obligation to publish it. After RabbitMQ confirms a persistent message, the
dispatcher stops publishing that operation. Celery's late acknowledgment handles
worker loss. A crash between confirmation and saving the sent marker can still
produce a duplicate after lease expiry; transactional application makes it safe.
Lease tokens prevent a late publisher from overwriting a worker's newer retry.

Backlog alone does not generate extra copies. Failed publication backs off and
repeated execution errors are parked for operator review. A catastrophic loss of
RabbitMQ's persistent data after confirmation still requires reconciliation of
sent-but-unprocessed operations. Automatic disaster recovery, queue admission
limits, and a full audited ledger are outside this exercise's scope.

Single-node MongoDB and RabbitMQ are development topology, not high availability.
Container image tags track patch updates; dependency locks do not pin base image
digests. Production needs authenticated users, tenant-scoped keys, authorization,
rate limits, TLS, managed secrets, least-privilege database/broker identities,
backups with restore tests, operational dashboards, deployment controls, and an
audited monetary ledger/reconciliation policy. No real payment rail is integrated.
The service is intended for local review with the included demo credentials.
