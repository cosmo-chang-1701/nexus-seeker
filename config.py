import os
from dotenv import load_dotenv

# 載入 .env 檔案
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_ADMIN_USER_ID = int(os.getenv("DISCORD_ADMIN_USER_ID", 0))
LOG_LEVEL = os.getenv("LOG_LEVEL", "WARNING").upper()

# 系統與模型參數
RISK_FREE_RATE = 0.042
DB_NAME = "data/nexus_data.db"

# 策略目標 Delta 參數
TARGET_DELTAS = {
    "STO_PUT": -0.20,
    "STO_CALL": 0.20,
    "BTO_PUT": -0.50,
    "BTO_CALL": 0.50
}