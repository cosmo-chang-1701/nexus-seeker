version = 88
description = "整批清空 historical_iv，改由盤中即時 IV (LIVE_IV) 從頭累積 IV Rank 母體"

# 背景 (數據正確性修正 M4)：fetch_and_calculate_iv_metrics 過去在盤後/週末也以
# 「今天」日期寫入 STORED_IV (前值搬運) 與 HV_PROXY (已實現波動率)，使 IV Rank
# 母體混入週末列、與前一筆完全相同的搬運列，以及無來源欄位可辨識的 HV_PROXY 列。
# 現已改為只在盤中寫入即時 IV (LIVE_IV)。
#
# HV_PROXY 列無法與真實 IV 區分，逐列清理無法保證母體乾淨，故整批清空。
# 清空後約 60 個交易日內 IVR 為未知 (樣本不足)，IVR 相關閘門 (賣方鎖定、
# Covered Call 解鎖、target_ivr) 走未知路徑。
#
# 刻意寫死 SQL：遷移必須是當下的凍結快照。
sql = """
DELETE FROM historical_iv;
"""
