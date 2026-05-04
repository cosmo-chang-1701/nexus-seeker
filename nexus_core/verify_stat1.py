
def check_trigger(usd_value, dynamic_threshold, static_threshold):
    meets_dynamic = usd_value >= dynamic_threshold
    meets_static = (static_threshold <= 0 or usd_value >= static_threshold)
    return meets_dynamic and meets_static

print("--- Docker 內環境 Polymarket 邏輯驗證 (Static=$1) ---")
test_cases = [
    {"name": "低於動態但高於靜態 ($500)", "val": 500.0, "dyn": 1000.0, "stat": 1.0, "expected": False},
    {"name": "高於兩者 ($2604)", "val": 2604.75, "dyn": 1000.0, "stat": 1.0, "expected": True},
]

for tc in test_cases:
    res = check_trigger(tc["val"], tc["dyn"], tc["stat"])
    status = "OK" if res == tc["expected"] else "FAIL"
    print(f"[{tc['name']}] 金額:${tc['val']} | 動態:${tc['dyn']} | 靜態:${tc['stat']} -> 結果: {'🚩 觸發' if res else '⚪ 過濾'} | {status}")
