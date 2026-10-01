version = 88
description = (
    "清理 historical_iv 的污染列：週末日期列，以及與前一筆完全相同的搬運列 "
    "(STORED_IV 前值搬運)"
)

# 背景 (數據正確性修正 M4)：fetch_and_calculate_iv_metrics 過去在盤後/週末也以
# 「今天」日期寫入 STORED_IV (前值搬運) 與 HV_PROXY (已實現波動率)，使 IV Rank
# 母體出現 (1) 週末日期列、(2) 與前一筆 IV 完全相同的搬運列。現已改為只在盤中
# 寫入即時 IV (LIVE_IV)。
#
# - 週末列：美股週末不開盤，必為非即時值，直接刪除。
# - 搬運列：STORED_IV 寫入的是「上一筆已存 IV」，與前一筆 (依日期) 的值逐位元
#   相同；兩個不同交易日的即時加權 IV 完全相等在實務上不會發生，故刪除與前一筆
#   相等的列 (保留第一筆)。
# - 國定假日列由讀取端 (iv_metrics 以 NYSE 交易日窗口過濾) 排除，不在此刪除。
# - HV_PROXY 列沒有來源欄位可辨識 (只會出現在標的首筆歷史之前)，無法安全刪除，
#   留待人工決定是否整批重建。
#
# 刻意寫死 SQL：遷移必須是當下的凍結快照。
sql = """
DELETE FROM historical_iv
WHERE CAST(strftime('%w', date) AS INTEGER) IN (0, 6);

DELETE FROM historical_iv
WHERE rowid IN (
    SELECT rowid FROM (
        SELECT
            rowid,
            iv,
            LAG(iv) OVER (PARTITION BY symbol ORDER BY date) AS prev_iv
        FROM historical_iv
    )
    WHERE prev_iv IS NOT NULL AND iv = prev_iv
);
"""
