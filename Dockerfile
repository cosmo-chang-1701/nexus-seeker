FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 複製需求文件
COPY requirements.txt /app/

# 更新系統套件庫，並安裝 Python 套件
RUN apt-get update && \
    pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 安裝 Chromium 核心與作業系統相依函式庫
# install-deps 會自動分析並透過 apt-get 下載缺少的 Linux 系統套件
RUN playwright install chromium && \
    playwright install-deps chromium && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# 複製應用程式碼
COPY . /app/

# 容器健康檢查
HEALTHCHECK --interval=1m --timeout=10s --start-period=1m --retries=3 \
    CMD python -c "import os; os.kill(1, 0)" || exit 1

# 執行主程式
CMD ["python", "main.py"]