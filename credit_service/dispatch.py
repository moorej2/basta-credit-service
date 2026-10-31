"""Publish durable outbox entries; confirmed messages belong to RabbitMQ."""

import logging
import signal
from threading import Event

from credit_service.celery_app import celery_app
from credit_service.config import get_settings
from credit_service.repository import CreditRepository

logger = logging.getLogger(__name__)


def publish(credit_id: str):
    celery_app.send_task("credits.apply", args=[credit_id], task_id=credit_id)


def dispatch_one(repo: CreditRepository, credit_id: str | None = None) -> bool:
    record = repo.claim_dispatch(credit_id)
    if record is None:
        return False
    # A crash before OR after publish is safe: expiry permits republication.
    publish(record["_id"])
    repo.mark_published(record["_id"], record["dispatch_token"])
    logger.info("credit_dispatched task_id=%s", record["_id"])
    return True


def main():
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    repo = CreditRepository(settings)
    stopped = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    try:
        while not stopped.is_set():
            try:
                for _ in range(100):  # Bound batches so shutdown remains responsive.
                    if stopped.is_set() or not dispatch_one(repo):
                        break
            except Exception:
                logger.exception("dispatch_failed; pending credits will be retried")
            stopped.wait(settings.dispatch_interval_seconds)
    finally:
        repo.close()


if __name__ == "__main__":
    main()
