from decimal import Decimal

import pytest
from pydantic import ValidationError

from credit_service.models import CreditRequest


@pytest.mark.parametrize("amount", ["0", "-1", "1.001", "NaN", "Infinity", "1000000000.01"])
def test_rejects_invalid_money(amount):
    with pytest.raises(ValidationError):
        CreditRequest(user_id="u_123", amount=amount)


@pytest.mark.parametrize("user_id", ["", " ", "user/123", "x" * 129])
def test_rejects_invalid_user(user_id):
    with pytest.raises(ValidationError):
        CreditRequest(user_id=user_id, amount="1.00")


def test_preserves_decimal_precision():
    credit = CreditRequest(user_id="u_123", amount="0.10")
    assert credit.amount == Decimal("0.10")


def test_rejects_unexpected_fields():
    with pytest.raises(ValidationError):
        CreditRequest(user_id="u_123", amount="1", admin=True)
