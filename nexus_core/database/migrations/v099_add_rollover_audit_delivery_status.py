version = 99
description = (
    "新增 rollover_audit_log.delivery_status：記錄該筆轉倉建議是否已排入推播佇列"
    "（NULL = 本欄上線前的舊列，無法得知）"
)

# 不回填舊列：表內沒有任何欄位可推回當時是否推播，NULL 代表未知。
sql = "ALTER TABLE rollover_audit_log ADD COLUMN delivery_status TEXT;"
