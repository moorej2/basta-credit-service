from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

UserId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
Money = Annotated[Decimal, Field(gt=0, le=Decimal("1000000000.00"), decimal_places=2)]


class CreditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: UserId
    amount: Money


class CreditResponse(BaseModel):
    task_id: str
    user_id: str
    amount: str
    status: Literal["processing", "processed", "failed"]
    error: str | None = None


class UserResponse(BaseModel):
    user_id: str
    balance: str
    status: Literal["processing", "processed", "failed"]
    pending_credits: int
    failed_credits: int


def money_string(value: Decimal) -> str:
    return format(value, ".2f")
