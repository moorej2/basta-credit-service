# Verification evidence

Verified locally on October 31, 2026 after the outbox/retry corrections. These
are local results, not a production deployment. Remote runs are available in
[GitHub Actions](https://github.com/moorej2/basta-credit-service/actions/workflows/ci.yml);
check the run for the commit being reviewed.

## Checks executed

| Check | Result |
| --- | --- |
| `ruff check .` | Passed |
| `ruff format --check .` | Passed |
| Unit tests without infrastructure | 31 passed, 40 deselected |
| Compose startup with build and readiness checks | Passed |
| Full Docker-backed suite | 71 passed in 8.38 seconds, excluding build/startup time |
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
   in approximately 0.01 seconds in this run. This is one observation, not a
   latency benchmark or a hard response-time guarantee.
4. Restarted RabbitMQ. The existing worker reconnected and the dispatcher
   recovered the pending credit. The final balance was exactly 2.50, status
   processed, pending count zero. No manual republication was required.

Recorded identifiers for the recovery run:

- User: `recovery_3102a6865fcb434e9225747ddd40a30b`
- Worker restart credit: `63c1ee9c-9687-4b71-a8cd-09b2482184ea`
- Broker outage credit: `29c6b951-3ccd-45cc-8456-b5169dbb5c13`

The script restores both services in a finally block and leaves the demo running.

## Regression and failure coverage

- Numeric BSON double/integer/Decimal128 balances, absent balance fields, and
  missing counters can be read; credits apply to legacy numeric balances.
- Repeated and concurrent requests/tasks preserve the exact balance and counters.
- An exception between balance mutation and completion rolls back the transaction.
- Confirmed messages are not republished while waiting in a queue. Unconfirmed
  publication backs off to a cap without declaring a waiting credit failed.
- Lease tokens reject stale publication acknowledgments and protect a retry
  recorded by a worker before publication confirmation is saved.
- Repeated actual execution errors park a credit; requeue resets its budget.
  A real MongoDB invalid-balance error exercises the complete task handler.
- Infrastructure and write-concern/uncertain-commit errors preserve the failure
  budget. A simulated lost reply after a real commit cannot change the balance
  or reclassify the operation as failed.
- When retry intent cannot be persisted, Celery schedules a broker retry. The
  real Celery retry implementation is exercised with a failed publish; the
  original delivery is rejected with requeue enabled.
- The failure transition racing a successful transaction leaves one consistent
  outcome; a missing user cannot produce a false processed marker.
- Worker clients are reused within a process, recreated after a PID change, and
  closed at worker shutdown.
- Dispatcher tests cover idle waiting, recovery after an exception, signal-driven
  shutdown, and client cleanup. CLI tests cover status/age, successful recovery,
  refusing processed operations, missing arguments, and cleanup.
- Readiness failures return 503 without POST-specific idempotency advice. API
  acceptance does not contact RabbitMQ.

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
