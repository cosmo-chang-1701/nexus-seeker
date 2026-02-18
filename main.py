import logging
import asyncio
import signal
import sys
import database
from config import DISCORD_TOKEN, LOG_LEVEL
from bot import NexusBot

# 0. 設定日誌
logging.basicConfig(level=getattr(logging, LOG_LEVEL), format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger(__name__)

# 1. 初始化資料庫
database.init_db()

async def main():
    if not DISCORD_TOKEN:
        logger.error("❌ 錯誤：找不到 DISCORD_TOKEN。")
        return

    bot = NexusBot()

    # 取得當前的 event loop
    loop = asyncio.get_running_loop()

    # 定義訊號處理器
    def handle_signal():
        logger.info("收到停止訊號 (SIGINT/SIGTERM)，正在發送關閉通知...")
        # 建立一個 task 來執行 bot.close()，這會觸發 bot.close() 中的通知邏輯
        asyncio.create_task(bot.close())

    # 註冊訊號 (注意：Windows 上不支援 add_signal_handler，但在 Docker/Linux 環境下是最佳實踐)
    try:
        loop.add_signal_handler(signal.SIGINT, handle_signal)
        loop.add_signal_handler(signal.SIGTERM, handle_signal)
    except NotImplementedError:
        logger.warning("當前環境不支援 add_signal_handler (可能是 Windows)，將使用預設訊號處理。")
        # 如果是在 Windows 開發，可以 fallback 到 signal.signal，但通常建議在 WSL/Linux 下運行
        pass

    async with bot:
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # 正常退出時忽略 KeyboardInterrupt
        pass