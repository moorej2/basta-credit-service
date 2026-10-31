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
            socketTimeoutMS=10000,
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
        self.credits.create_index([("status", ASCENDING), ("next_dispatch_at", ASCENDING)])

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
        query = {"status": "processing", "next_dispatch_at": {"$lte": now}}
        if credit_id is not None:
            query["_id"] = credit_id
        return self.credits.find_one_and_update(
            query,
            {
                "$set": {
                    "next_dispatch_at": now
                    + timedelta(seconds=self.settings.dispatch_lease_seconds)
                },
                "$inc": {"dispatch_count": 1},
            },
            sort=[("next_dispatch_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )

    def finish(self, credit_id: str, *, error: str | None = None) -> bool:
        """Atomically apply once, or record a permanent failure without adding money."""
        now = datetime.now(UTC)

        def apply(session):
            credit = self.credits.find_one({"_id": credit_id}, session=session)
            if credit is None or credit["status"] != "processing":
                return False
            increments = {"pending_credits": -1}
            if error is None:
                increments["balance"] = credit["amount"]
            else:
                increments["failed_credits"] = 1
            self.users.update_one(
                {"user_id": credit["user_id"]},
                {"$inc": increments, "$set": {"updated_at": now}},
                session=session,
            )
            # A pipeline computes aggregate status from the UPDATED counters.
            self.users.update_one(
                {"user_id": credit["user_id"]},
                [
                    {
                        "$set": {
                            "status": {
                                "$cond": [
                                    {"$gt": ["$pending_credits", 0]},
                                    "processing",
                                    {
                                        "$cond": [
                                            {"$gt": ["$failed_credits", 0]},
                                            "failed",
                                            "processed",
                                        ]
                                    },
                                ]
                            }
                        }
                    }
                ],
                session=session,
            )
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
                    }
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
