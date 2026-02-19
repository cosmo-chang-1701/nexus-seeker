FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/

RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . /app/

HEALTHCHECK --interval=1m --timeout=10s --start-period=1m --retries=3 \
    CMD python -c "import os; os.kill(1, 0)" || exit 1

CMD ["python", "main.py"]