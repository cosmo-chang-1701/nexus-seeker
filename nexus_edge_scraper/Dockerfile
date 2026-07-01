FROM python:3.12-slim

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

RUN mkdir -p /ms-playwright && \
    playwright install chromium && \
    playwright install-deps chromium

COPY local_api.py .

RUN groupadd -g 1001 appuser && \
    useradd -u 1001 -g appuser -s /bin/bash -m appuser && \
    chown -R appuser:appuser /app /ms-playwright

USER appuser

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "local_api:app", "--host", "0.0.0.0", "--port", "8000"]
