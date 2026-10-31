import os
from uuid import uuid4

import pytest

from credit_service.config import Settings
from credit_service.repository import CreditRepository


@pytest.fixture
def repo():
    # Tests never drop the production or shared test database.
    if not os.getenv("CREDIT_MONGO_URI"):
        pytest.skip("Set CREDIT_MONGO_URI or run the Docker Compose test service")
    settings = Settings(mongo_database=f"credits_test_{uuid4().hex}")
    repository = CreditRepository(settings)
    repository.initialize()
    try:
        yield repository
    finally:
        repository.client.drop_database(settings.mongo_database)
        repository.close()
