"""MongoDB is authoritative for money, operation state, and pending dispatch.

Transaction callbacks contain database work only: PyMongo can replay them.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from bson.decimal128 import Decimal128
from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from credit_service.config import Settings


class IdempotencyConflict(Exception):
    pass


class CreditRepository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = MongoClient(
            settings.mongo_uri,
            serverSelectionTimeoutMS=settings.mongo_timeout_ms,
            connectTimeoutMS=settings.mongo_timeout_ms,
            socketTimeoutMS=settings.mongo_socket_timeout_ms,
            tz_aware=True,
        )
        self.db = self.client[settings.mongo_database]
        self.users = self.db.users
        self.credits = self.db.credits

    def close(self):
        self.client.close()

    def initialize(self):
        if not self.client.admin.command("hello").get("setName"):
            raise RuntimeError("MongoDB must run as a replica set for transactions")
        self.users.create_index("user_id", unique=True)
        self.credits.create_index("idempotency_key", unique=True)
        self.credits.create_index(
            [("status", ASCENDING), ("dispatch_state", ASCENDING), ("next_dispatch_at", ASCENDING)]
        )

    def transaction(self, callback):
        with self.client.start_session() as session:
            return session.with_transaction(
                callback,
                read_concern=ReadConcern("snapshot"),
                write_concern=WriteConcern("majority"),
            )

    def accept(self, user_id: str, amount: Decimal, key: str) -> dict:
        credit_id = str(uuid4())
        now = datetime.now(UTC)

        def existing_or_conflict(existing):
            if existing["user_id"] != user_id or existing["amount"].to_decimal() != amount:
                raise IdempotencyConflict(
                    "Idempotency key already used with different request data"
                )
            return existing

        def save(session):
            existing = self.credits.find_one({"idempotency_key": key}, session=session)
            if existing:
                return existing_or_conflict(existing)
            record = {
                "_id": credit_id,
                "idempotency_key": key,
                "user_id": user_id,
                "amount": Decimal128(amount),
                "status": "processing",
                "created_at": now,
                "updated_at": now,
                "next_dispatch_at": now,
                "dispatch_count": 0,
                "dispatch_attempts": 0,
                "dispatch_state": "pending",
                "failure_count": 0,
                "retry_count": 0,
            }
            self.credits.insert_one(record, session=session)
            self.users.update_one(
                {"user_id": user_id},
                [
                    {
                        "$set": {
                            "failed_credits": {"$ifNull": ["$failed_credits", 0]},
                            "pending_credits": {"$add": [{"$ifNull": ["$pending_credits", 0]}, 1]},
                            "status": "processing",
                            "updated_at": now,
                        }
                    }
                ],
                upsert=True,
                session=session,
            )
            return record

        # Concurrent first requests can race on either unique index. Retry the
        # transaction with a fresh snapshot; never perform broker I/O inside it.
        for attempt in range(5):
            try:
                return self.transaction(save)
            except DuplicateKeyError:
                if attempt == 4:
                    raise
        raise AssertionError("unreachable")

    def get_credit(self, credit_id: str):
        return self.credits.find_one({"_id": credit_id})

    def get_user(self, user_id: str):
        return self.users.find_one({"user_id": user_id})

    def claim_dispatch(self, credit_id: str | None = None):
        now = datetime.now(UTC)
        query = {
            "status": "processing",
            "dispatch_state": {"$ne": "sent"},
            "next_dispatch_at": {"$lte": now},
        }
        if credit_id is not None:
            query["_id"] = credit_id
        attempts = {"$min": [{"$ifNull": ["$dispatch_attempts", 0]}, 16]}
        delay_seconds = {
            "$min": [
                self.settings.dispatch_backoff_max_seconds,
                {"$multiply": [self.settings.dispatch_lease_seconds, {"$pow": [2, attempts]}]},
            ]
        }
        return self.credits.find_one_and_update(
            query,
            [
                {
                    "$set": {
                        "next_dispatch_at": {"$add": [now, {"$multiply": [1000, delay_seconds]}]},
                        "dispatch_count": {"$add": [{"$ifNull": ["$dispatch_count", 0]}, 1]},
                        "dispatch_attempts": {"$add": [{"$ifNull": ["$dispatch_attempts", 0]}, 1]},
                        "dispatch_token": str(uuid4()),
                    }
                }
            ],
            sort=[("next_dispatch_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )

    def mark_published(self, credit_id: str, token: str) -> bool:
        # A fast worker may already have completed or scheduled a retry. A stale
        # publisher must never overwrite that newer state.
        result = self.credits.update_one(
            {"_id": credit_id, "status": "processing", "dispatch_token": token},
            {
                "$set": {"dispatch_state": "sent", "dispatch_attempts": 0},
                "$unset": {"dispatch_token": ""},
            },
        )
        return result.modified_count == 1

    def _finish_user(self, credit: dict, now: datetime, error: str | None, session):
        increments = {"pending_credits": -1}
        increments["balance" if error is None else "failed_credits"] = (
            credit["amount"] if error is None else 1
        )
        result = self.users.update_one(
            {"user_id": credit["user_id"], "pending_credits": {"$gt": 0}},
            {"$inc": increments, "$set": {"updated_at": now}},
            session=session,
        )
        if result.matched_count != 1:
            raise RuntimeError("Credit has no user with a pending operation")
        self.users.update_one(
            {"user_id": credit["user_id"]},
            [
                {
                    "$set": {
                        "status": {
                            "$cond": [
                                {"$gt": ["$pending_credits", 0]},
                                "processing",
                                {"$cond": [{"$gt": ["$failed_credits", 0]}, "failed", "processed"]},
                            ]
                        }
                    }
                }
            ],
            session=session,
        )

    def defer(self, credit_id: str, error: str, *, count_failure: bool) -> str:
        """Persist retry intent or park repeated execution errors, never queue age.

        Reading and writing the credit in this transaction also arbitrates with
        an uncertain/parallel successful commit. Processed money is never reopened.
        """
        now = datetime.now(UTC)

        def save(session):
            credit = self.credits.find_one({"_id": credit_id}, session=session)
            if credit is None or credit["status"] != "processing":
                return "terminal"
            failures = credit.get("failure_count", 0) + int(count_failure)
            retries = credit.get("retry_count", 0) + 1
            parked = failures >= self.settings.task_max_failures
            delay = min(
                self.settings.task_retry_max_seconds,
                self.settings.task_retry_base_seconds * 2 ** min(retries - 1, 16),
            )
            if parked:
                self._finish_user(credit, now, error, session)
            self.credits.update_one(
                {"_id": credit_id},
                {
                    "$set": {
                        "status": "failed" if parked else "processing",
                        "dispatch_state": "pending",
                        "dispatch_attempts": 0,
                        "failure_count": failures,
                        "retry_count": retries,
                        "error": error,
                        "updated_at": now,
                        "next_dispatch_at": now + timedelta(seconds=delay),
                    },
                    "$unset": {"dispatch_token": ""},
                },
                session=session,
            )
            return "failed" if parked else "retry"

        return self.transaction(save)

    def finish(self, credit_id: str, *, error: str | None = None) -> bool:
        """Atomically apply once, or record a permanent failure without adding money."""
        now = datetime.now(UTC)

        def apply(session):
            credit = self.credits.find_one({"_id": credit_id}, session=session)
            if credit is None or credit["status"] != "processing":
                return False
            self._finish_user(credit, now, error, session)
            self.credits.update_one(
                {"_id": credit_id},
                {
                    "$set": {
                        "status": "processed" if error is None else "failed",
                        "error": error,
                        "updated_at": now,
                    }
                },
                session=session,
            )
            return True

        return self.transaction(apply)

    def requeue_failed(self, credit_id: str) -> bool:
        now = datetime.now(UTC)

        def requeue(session):
            credit = self.credits.find_one({"_id": credit_id}, session=session)
            if credit is None or credit["status"] != "failed":
                return False
            self.credits.update_one(
                {"_id": credit_id},
                {
                    "$set": {
                        "status": "processing",
                        "error": None,
                        "next_dispatch_at": now,
                        "updated_at": now,
                        "dispatch_state": "pending",
                        "dispatch_attempts": 0,
                        "failure_count": 0,
                        "retry_count": 0,
                    },
                    "$unset": {"dispatch_token": ""},
                },
                session=session,
            )
            self.users.update_one(
                {"user_id": credit["user_id"]},
                {
                    "$inc": {"pending_credits": 1, "failed_credits": -1},
                    "$set": {"status": "processing", "updated_at": now},
                },
                session=session,
            )
            return True

        return self.transaction(requeue)
