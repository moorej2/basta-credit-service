FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.lock .
RUN pip install -r requirements.lock
COPY pyproject.toml .
COPY credit_service ./credit_service
RUN pip install --no-deps . && useradd --uid 10001 --create-home app
USER app
CMD ["uvicorn", "credit_service.api:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS test
USER root
COPY requirements-dev.lock .
RUN pip install -r requirements-dev.lock
COPY tests ./tests
USER app
ENV PYTEST_ADDOPTS="-p no:cacheprovider"
CMD ["pytest", "-q"]
