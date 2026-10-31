import logging
import os
import random

from celery.exceptions import Reject
from celery.signals import worker_process_shutdown
from kombu.exceptions import OperationalError
from pymongo.errors import ConnectionFailure, OperationFailure, PyMongoError, WriteConcernError

from credit_service.celery_app import celery_app
from credit_service.config import get_settings
from credit_service.repository import CreditRepository

logger = logging.getLogger(__name__)
_worker_repository: tuple[int, CreditRepository] | None = None

# Errors which can reflect failover, contention, or temporary resource limits.
_RETRYABLE_CODES = {
    6,
    7,
    50,
    64,
    89,
    91,
    112,
    189,
    244,
    251,
    262,
    9001,
    10107,
    11600,
    11602,
    13435,
    13436,
}


def get_worker_repository() -> CreditRepository:
    global _worker_repository
    pid = os.getpid()
    if _worker_repository is None or _worker_repository[0] != pid:
        # A parent-created client is never used or closed in a forked child.
        _worker_repository = (pid, CreditRepository(get_settings()))
    return _worker_repository[1]


@worker_process_shutdown.connect
def close_worker_repository(**kwargs):
    global _worker_repository
    if _worker_repository is not None and _worker_repository[0] == os.getpid():
        _worker_repository[1].close()
    _worker_repository = None


def transient_database_error(exc: Exception) -> bool:
    return isinstance(exc, (ConnectionFailure, WriteConcernError)) or (
        isinstance(exc, OperationFailure)
        and (
            exc.code in _RETRYABLE_CODES
            or exc.has_error_label("TransientTransactionError")
            or exc.has_error_label("UnknownTransactionCommitResult")
        )
    )


@celery_app.task(bind=True, name="credits.apply", max_retries=None)
def apply_credit(self, credit_id: str):
    repo = get_worker_repository()
    try:
        changed = repo.finish(credit_id)
        logger.info("credit_completed task_id=%s changed=%s", credit_id, changed)
        return
    except Exception as exc:
        infrastructure = transient_database_error(exc)
        error = "database_unavailable" if infrastructure else "task_execution_failed"
        logger.exception(
            "credit_attempt_failed task_id=%s retryable_database=%s", credit_id, infrastructure
        )
        try:
            outcome = repo.defer(credit_id, error, count_failure=not infrastructure)
        except PyMongoError:
            # MongoDB cannot save retry intent. Keep work in RabbitMQ instead.
            settings = get_settings()
            delay = random.uniform(
                1,
                min(
                    settings.task_retry_max_seconds,
                    settings.task_retry_base_seconds * 2 ** min(self.request.retries, 16),
                ),
            )
            try:
                raise self.retry(exc=exc, countdown=delay) from exc
            except (OperationalError, Reject) as publish_error:
                # Do not acknowledge the original if BOTH stores are unreachable.
                raise Reject(str(publish_error), requeue=True) from publish_error
        logger.info("credit_retry_recorded task_id=%s outcome=%s", credit_id, outcome)
