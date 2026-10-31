import logging
import random

from pymongo.errors import ConnectionFailure, OperationFailure

from credit_service.celery_app import celery_app
from credit_service.config import get_settings
from credit_service.repository import CreditRepository

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, name="credits.apply", max_retries=None)
def apply_credit(self, credit_id: str):
    # Construct AFTER Celery forks; don't share a parent process MongoClient.
    repo = CreditRepository(get_settings())
    try:
        changed = repo.finish(credit_id)
        logger.info("credit_completed task_id=%s changed=%s", credit_id, changed)
    except ConnectionFailure as exc:
        # Infrastructure outages must not silently discard accepted money operations.
        delay = random.uniform(1, min(60, 2 ** min(self.request.retries + 1, 6)))
        raise self.retry(exc=exc, countdown=delay) from exc
    except OperationFailure as exc:
        if exc.has_error_label("TransientTransactionError") or exc.has_error_label(
            "UnknownTransactionCommitResult"
        ):
            raise self.retry(exc=exc, countdown=5) from exc
        # A permanent database/schema problem requires explicit operator recovery.
        logger.exception("credit_failed task_id=%s code=%s", credit_id, exc.code)
        repo.finish(credit_id, error="database_operation_failed")
    finally:
        repo.close()
