FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

RUN playwright install chromium && \
    playwright install-deps chromium

COPY local_api.py .

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "local_api:app", "--host", "0.0.0.0", "--port", "8000"]