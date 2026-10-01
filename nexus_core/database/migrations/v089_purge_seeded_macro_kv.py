version = 89
description = "清除 kv_cache 中仍停留在 v050 種子預設值的總經指標列"

# 背景 (數據正確性修正)：v050 以 INSERT OR IGNORE 為總經指標種下常數預設值
# (例如 sahm 0.35、US10Y 4.25、VIX 18、Fear & Greed 48)。部分鍵只在 edge
# scraper 成功回傳時才會被覆寫 (sahm/uer/rrp/fed_balance/fear_greed/
# rrp_change_30d)，macro_cpi_deviation 與 macro_cpi_nfp_calendar 則沒有任何
# 寫入端；未設定 TUNNEL_URL 的部署會永遠把這些常數當成真實讀值 (例如 Covered
# Call 衰退閘門讀到 sahm 0.35 判定「非衰退」)。
#
# 只刪除「值仍與種子逐字相同」的列：真實值恰好等於種子時也會被刪除，但下一次
# 成功抓取就會重新寫入，期間由讀取端走未知路徑，屬可接受的暫時狀態。
#
# 刻意寫死 SQL：遷移必須是當下的凍結快照。
sql = """
DELETE FROM kv_cache WHERE (key, value) IN (
    VALUES
    ('macro_spx', '5150.0'),
    ('macro_vix', '18.0'),
    ('macro_us10y', '4.25'),
    ('macro_wti', '75.0'),
    ('macro_rrp', '420.5'),
    ('macro_fed_balance', '7.25'),
    ('macro_cpi_nfp_calendar', '"2026-06-18 (CPI), 2026-07-03 (NFP)"'),
    ('macro_fear_greed', '48.0'),
    ('macro_gamma_flip_line', '5180.0'),
    ('macro_spy_spot', '510.0'),
    ('macro_spy_gamma_flip', '515.0'),
    ('macro_vts_ratio', '0.95'),
    ('macro_uer', '4.0'),
    ('macro_sahm_rule', '0.35'),
    ('macro_cpi_deviation', '0.0'),
    ('macro_rrp_change_30d', '0.05')
);
"""
