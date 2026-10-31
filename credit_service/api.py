import logging
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

from bson.decimal128 import Decimal128
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from credit_service.config import get_settings
from credit_service.dispatch import dispatch_one
from credit_service.models import CreditRequest, CreditResponse, UserResponse, money_string
from credit_service.repository import CreditRepository, IdempotencyConflict

logger = logging.getLogger(__name__)


def credit_response(record: dict) -> CreditResponse:
    return CreditResponse(
        task_id=record["_id"],
        user_id=record["user_id"],
        amount=money_string(record["amount"].to_decimal()),
        status=record["status"],
        error=record.get("error"),
    )


def create_app(repository: CreditRepository | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        logging.basicConfig(level=logging.INFO)
        repo = repository or CreditRepository(get_settings())
        # Startup only; routes below use def so blocking I/O runs in a thread pool.
        repo.initialize()
        app.state.repo = repo
        yield
        if repository is None:
            repo.close()

    app = FastAPI(title="Async Credit Service", version="1.0.0", lifespan=lifespan)

    @app.exception_handler(PyMongoError)
    async def mongo_error(request, exc):
        logger.error("database_unavailable path=%s type=%s", request.url.path, type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={"detail": "Database unavailable; retry safely using the same Idempotency-Key"},
        )

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready(request: Request):
        # API can durably accept work during broker outages; MongoDB is required.
        request.app.state.repo.client.admin.command("ping")
        return {"status": "ready"}

    @app.post("/credits", status_code=202, response_model=CreditResponse)
    def create_credit(
        body: CreditRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None, Header(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
        ] = None,
    ):
        repo = request.app.state.repo
        try:
            record = repo.accept(body.user_id, body.amount, idempotency_key or str(uuid4()))
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            dispatch_one(repo, record["_id"])
        except Exception:
            # Acceptance already committed. Don't report a rejection or lose work.
            logger.exception("initial_dispatch_failed task_id=%s", record["_id"])
        response.headers["Location"] = f"/credits/{record['_id']}"
        logger.info("credit_accepted task_id=%s", record["_id"])
        return credit_response(record)

    @app.get("/credits/{task_id}", response_model=CreditResponse)
    def get_credit(task_id: str, request: Request):
        record = request.app.state.repo.get_credit(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Credit not found")
        return credit_response(record)

    @app.get("/users/{user_id}", response_model=UserResponse)
    def get_user(user_id: str, request: Request):
        record = request.app.state.repo.get_user(user_id)
        if record is None:
            raise HTTPException(status_code=404, detail="User not found")
        return UserResponse(
            user_id=record["user_id"],
            balance=money_string(record.get("balance", Decimal128("0")).to_decimal()),
            status=record["status"],
            pending_credits=record["pending_credits"],
            failed_credits=record["failed_credits"],
        )

    return app


app = create_app()
