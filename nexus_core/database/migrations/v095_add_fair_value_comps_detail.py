version = 95
description = "fair_value_log 新增 comps_detail_json（同業乘數法輸入，供稽核）"
# 注意：
# - 只對 v094 建立的 fair_value_log 做 ADD COLUMN（v094 已發版，不得修改）。
# - 欄位可為 NULL：舊資料與無同業乘數輸入時存 NULL。
sql = """
ALTER TABLE fair_value_log ADD COLUMN comps_detail_json TEXT;
"""
