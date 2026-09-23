"""GEX 牆體深度（薄牆）門檻：stdlib 葉模組。

刻意只依賴標準函式庫（比照 `room_threshold.py`），讓 `index_microstructure`、
`dynamic_rollover/`、`insights_engine`、`scenario_classifier` 與各 embed 都能
匯入同一個定義而不產生循環相依。
"""

import math
from typing import Optional

# 薄牆門檻 (D-04)。GEX 原始值為 edge 的 `OI × 100 × Γ × S²`，是「每 100% 價格
# 變動」的尺度，量級隨股價平方與合約數成長，固定絕對門檻對大型股形同虛設。
# 門檻取兩者較大值：
#   threshold_raw = max(GEX_THIN_WALL_THRESHOLD, GEX_WALL_MIN_DEPTH_RATIO × ADV × 100)
# 第二項是「牆體深度比」：每 1% 價格變動的做市商避險名目 (raw × 0.01) ÷ 20 日平均
# 成交額。絕對下限保留原本的 500k：避險名目太小的牆不論標的多小都是紙牆 (RCAT 62K
# 案例，見 test_quant_radar_defects_fix.py)，所以正規化只會讓門檻**變嚴**，
# 只對 ADV > $500M 的標的生效。
# 深度比數值依 2026-09-22 的 94 檔橫斷面快照 (calibration micro-snapshot/
# micro-report)：深度比 < 1e-5 的「牆」幾乎都距現價 8~66% (雜訊牆)，>= 3e-5 的都
# 貼近現價；1e-5 不再放行大型股遠處的薄牆 (ORCL/CRM/XOM/COST 等)。
# PRE_CALIBRATION：守住率需以 micro-report 逐日累積的標註資料驗證。
GEX_WALL_MIN_DEPTH_RATIO: float = 1e-5
# 絕對下限 (原始單位)；成交額未知時即為門檻，行為與改版前相同。
GEX_THIN_WALL_THRESHOLD: float = 500_000.0


def thin_wall_threshold(adv_dollar_20d: Optional[float] = None) -> float:
    """回傳薄牆門檻 (GEX 原始單位)；成交額未知或無效時為絕對下限。"""
    try:
        adv = float(adv_dollar_20d) if adv_dollar_20d is not None else 0.0
    except (TypeError, ValueError):
        adv = 0.0
    if math.isfinite(adv) and adv > 0:
        return max(GEX_THIN_WALL_THRESHOLD, GEX_WALL_MIN_DEPTH_RATIO * adv * 100.0)
    return GEX_THIN_WALL_THRESHOLD
