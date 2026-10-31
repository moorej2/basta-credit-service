# Verification evidence

Verified locally on October 31, 2026. These are actual local results, not a claim
of a production deployment or a successful remote GitHub Actions run.

## Checks executed

| Check | Result |
| --- | --- |
| `ruff check .` | Passed |
| `ruff format --check .` | Passed |
| Unit tests without infrastructure | 16 passed, 18 deselected |
| Compose startup with build and readiness checks | Passed |
| Full Docker-backed suite | 34 passed in 3.04 seconds, excluding build/startup time |
| Live worker-stop and restart exercise | Passed |
| Live RabbitMQ-stop and restart exercise | Passed |
| Two-page design PDF | Exactly 2 pages; both rendered pages visually inspected |

The full suite ran with real MongoDB 7.0 configured as a single-node replica set,
RabbitMQ 4.1, the FastAPI HTTP process, and a prefork Celery worker with two child
processes. Integration tests use separate disposable databases. The E2E test
submits 12 concurrent credits of 0.10 through the complete stack, repeats one
request, and checks a final balance of 1.20 with zero pending operations.

One upstream test-client deprecation warning was reported: the locked Starlette
version warns that its `httpx` TestClient compatibility path is deprecated in
favor of `httpx2`. All tests passed. This affects the testing dependency path;
future dependency refresh should migrate that adapter rather than suppress it.

## Real recovery exercise

Executed `scripts/check_recovery.py` against the running local stack:

1. Stopped the worker and submitted a credit of 1.25. The API returned 202 and the
   operation remained processing while no worker was available.
2. Restarted the worker. The credit completed. Repeating the same idempotency key
   returned the same credit ID and left the balance at 1.25.
3. Stopped RabbitMQ and submitted a second credit of 1.25. The API returned 202
   in approximately 0.03 seconds in this run. This is one observation, not a
   latency benchmark or a hard response-time guarantee.
4. Restarted RabbitMQ. The existing worker reconnected and the dispatcher
   recovered the pending credit. The final balance was exactly 2.50, status
   processed, pending count zero. No manual republication was required.

Recorded identifiers for the recovery run:

- User: `recovery_bc2dcb2195544f14a6ac7c6c9919867c`
- Worker restart credit: `3ffe8850-0fc9-4681-b209-e44a111dc23f`
- Broker outage credit: `39e8757b-6639-475f-b811-9c54bded36fa`

The script restores both services in a finally block and leaves the demo running.

## Tests with injected failures

- Raised an exception after the balance update but before the completion write:
  MongoDB rolled back the money and pending-count changes.
- Replayed an already committed operation: balance stayed unchanged.
- Executed a credit concurrently from eight threads: exactly one application.
- Submitted and processed 16 distinct credits concurrently for a new user:
  total balance and pending count remained correct.
- Failed publication after acquiring a dispatch lease, expired the lease, and
  verified the next dispatch attempt published the same operation ID.
- Verified permanent-failure status, counter transitions, and explicit requeue.
- Verified task retry classification and connection cleanup with unit tests.

## Boundaries of the evidence

- A simulated post-commit redelivery was tested; an OS-level kill at an
  instrumented commit boundary was not performed.
- No multi-node MongoDB primary election, RabbitMQ cluster failover, network
  partition, disk-full scenario, backup restoration, or sustained load test was run.
- Authentication, real financial settlement, and cloud deployment are outside
  this local exercise's implementation scope.
- Local Docker image tags are versioned but not pinned to immutable digests.

The README and design document describe the additional engineering work required
before a real production launch.
