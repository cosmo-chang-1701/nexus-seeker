FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Create a non-root user
RUN groupadd -g 1001 appuser && useradd -u 1001 -g appuser -s /bin/bash -m appuser

WORKDIR /app

COPY pyproject.toml /app/

RUN pip install --upgrade pip && \
    pip install --no-cache-dir . && \
    apt-get update && apt-get install -y --no-install-recommends gosu && rm -rf /var/lib/apt/lists/*

COPY . /app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Change ownership to the non-root user and initialize data dir for named volume
RUN mkdir -p /app/data && chown -R appuser:appuser /app


HEALTHCHECK --interval=1m --timeout=10s --start-period=1m --retries=3 \
    CMD python bot_healthy.py || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "main.py"]