"""部位保證金佔用量計算。

`calculate_option_margin` 名稱沿用歷史，實際上是全資產類別的保證金入口——
新增的 `stock` 分支讓空頭現貨也能回報真實的 Reg-T 佔用量。
"""

# 美國 Reg-T 空頭現貨初始保證金比例（法定 50%）。維持保證金為 30%，但本系統
# 的 portfolio_heat 是「開新倉前的煞車」，用初始保證金才是正確的量綱。
_REG_T_SHORT_STOCK_INITIAL_MARGIN_RATE: float = 0.50


def calculate_option_margin(
    opt_type: str,
    strike: float,
    current_stock_price: float,
    current_option_price: float,
    quantity: int,
    stock_cost: float = 0.0,
) -> float:
    """
    計算標的部位的保證金佔用量。

    `quantity >= 0` 一律回傳 0.0：多頭部位不佔用保證金（現金帳戶語意）。
    `abs(quantity)` 在此是**正確**的——保證金是量值，與方向無關，方向已由
    上方的正負號判斷消化掉。

    ⚠️ 本函式是全系統唯一的保證金模型，其輸出經 `total_margin_used` 匯總成
    `portfolio_heat`（risk_engine.get_macro_risk_metrics），而 heat 是「是否
    允許開新倉」的主要煞車。任何一種空頭部位若在此回傳 0.0，該煞車對它就
    完全失效。
    """
    if quantity >= 0:
        return 0.0

    abs_qty = abs(quantity)

    if opt_type == "call":
        if stock_cost > 0.0:
            # Covered Call
            return 0.0
        else:
            # Naked Call (簡化版保證金公式)
            otm = max(0, strike - current_stock_price)
            margin_locked = (
                max(
                    (0.20 * current_stock_price) - otm + current_option_price,
                    0.10 * current_stock_price + current_option_price,
                )
                * 100
                * abs_qty
            )
            return margin_locked
    elif opt_type == "put":
        # Cash Secured Put (簡化版：通常為 Strike * 100)
        # 若需要更精確的 Naked Put 公式可比照 Call
        return strike * 100 * abs_qty
    elif opt_type == "stock":
        # 空頭現貨 (Short Stock)：Reg-T 初始保證金 = 市值 × 50%。
        # 注意乘數是 1 而非 100——現貨以「股」計價，不是合約。
        #
        # 早期版本沒有這個分支，直接落到最後的 `return 0.0`，使空頭現貨部位
        # 的保證金佔用恆為零：一個純空頭帳戶的 portfolio_heat 會顯示 0%，
        # 而 30%/50% 的熱度警戒線正是系統阻止繼續開倉的主要防線。
        return current_stock_price * abs_qty * _REG_T_SHORT_STOCK_INITIAL_MARGIN_RATE

    return 0.0
