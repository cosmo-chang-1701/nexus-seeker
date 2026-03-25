FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Create a non-root user
RUN groupadd -g 1001 appuser && useradd -u 1001 -g appuser -s /bin/bash -m appuser

WORKDIR /app

COPY pyproject.toml /app/

RUN pip install --upgrade pip && \
    pip install --no-cache-dir .

COPY . /app/

# Change ownership to the non-root user and initialize data dir for named volume
RUN mkdir -p /app/data && chown -R appuser:appuser /app

# Switch to the non-root user
USER appuser

HEALTHCHECK --interval=1m --timeout=10s --start-period=1m --retries=3 \
    CMD python bot_healthy.py || exit 1

CMD ["python", "main.py"]