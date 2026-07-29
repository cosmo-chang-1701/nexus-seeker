# 系統架構 (Architecture)

Nexus Seeker 是一個多租戶的 Discord 期權風控與交易營運平台，專注於低內存 (Low-RAM) VPS 的部署，並採用 SQLite 快取機制與分散式服務來達成高效能與穩定性。

## 核心服務 (Services)

平台採用微服務（Microservices）架構：

1. **`nexus_core` (主機器人服務)**
   - **職責**：運行主要的 Discord Bot。
   - **功能**：負責所有 Slash 指令、背景排程、量化風險與投資組合計算邏輯、Watchlist 半小時戰場心跳，以及 Discord DM 訊息的佇列處理。
   - **資料庫**：SQLite (支援 migrations)。

2. **`nexus_edge_scraper` (邊緣爬蟲服務)**
   - **職責**：可選的 FastAPI 服務，搭配 Playwright 處理需要渲染的動態網頁。
   - **功能**：獲取 Reddit 輿情、解析大盤 (SPY) 選擇權鏈計算 GEX、獲取 CME FedWatch 利率機率等，避免阻擋 Bot 主執行緒的即時反應。
   - **安全**：透過 Cloudflare Tunnel 暴露，不直接曝露 Bot 所在的伺服器 IP。

## 非同步事件日曆架構 (Event Calendar Architecture)

為避免頻繁呼叫外部 API 導致被 Rate Limit 或增加回應延遲，平台實作了 `services/calendar_service.py` 共享快取層：
- **總經事件**：每月更新一次（由 `nexus_edge_scraper` 透過 TradingView 爬取）。
- **財報日曆**：依據標的 (Symbol) 快取（透過 Finnhub API）。
所有子系統（包含 Watchlist 心跳、日曆視圖、盤前警報與 Analyst Agent）皆共用同一個 SQLite 快取。

## 訊息交付佇列 (Persistent DM Queue)

`nexus_core/bot.py` 內建了強大的持久化私訊佇列 (Persistent DM Queue)：
- 推播通知會先進入佇列，確保啟動/關閉時的狀態保存與重試機制。
- 具備自動分段能力：當單一訊息超過 Discord 限制的 2000 字元時，會自動進行代碼區塊 (`code block`) 友善的切割。
- 所有主動推送（如 Watchlist 心跳）必須透過 `queue_dm` 進行。
