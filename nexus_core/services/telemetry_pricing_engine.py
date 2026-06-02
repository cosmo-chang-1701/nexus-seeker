import math
import logging

logger = logging.getLogger(__name__)


async def calculate_telemetry_price(
    symbol: str,
    base_price: float,
    spot_price: float,
    iv: float,
    hist_iv: float,
    max_pain: float,
    prev_max_pain: float,
    skew_percentile: float,
    days_to_expiration: float = 7.0,
    prev_close: float = 0.0,
    base_quantity: float = 1.0,
) -> tuple[float, float, list[str]]:
    """
    計算最佳「左側現股捕獸夾」遙測訂價，並將倉位控管動態連結至極端期權流指標。
    回傳: (最佳價格, 最佳股數, 決策日誌列表)
    """
    price = base_price
    logs = []
    symbol = symbol.upper()
    sizing_multiplier = 1.0

    # 1. 期權籌碼引力面 (Option Flow & Gravity)
    # 最大痛點位移
    if prev_max_pain > 0 and max_pain > prev_max_pain:
        scale = max_pain / prev_max_pain
        price = price * scale
        logs.append(
            f"📐 最大痛點位移：痛點上移 (${prev_max_pain:.2f} ➔ ${max_pain:.2f})，將掛單價格等比例上調至 ${price:.2f}。"
        )

    # 期權偏斜與情緒背離
    if skew_percentile < 0.05 or skew_percentile > 0.95:
        # 將價格調整至現價的 1.5% 處以捕捉恐慌/軋空盤影線
        if spot_price > price:
            price = spot_price * 0.985
        else:
            price = spot_price * 1.015

        # 連結優化：將分配預算減至 75%，防止激進攔截時資金過快枯竭
        sizing_multiplier = 0.75
        logs.append(
            f"[⚠️ 尾端風險防禦] 偵測到期權偏斜極端尾端風險 (百分位數 {skew_percentile*100:.1f}%)。"
            f"已將掛單價格微調至更接近現價 (${price:.2f})，且將掛單數量打 75 折以防禦尾部風險，保護資產流動性。"
        )

    # 2. 數學統計邊界面 (Statistical Boundaries & Volatility)
    # 計算預期區間 (Expected Move) 下限
    # EM = Spot * IV * sqrt(DTE/365)
    em_value = spot_price * iv * math.sqrt(days_to_expiration / 365.0)
    em_lower = spot_price - em_value

    # IV 暴噴工作流 (Vol Spike Workflow)
    is_vol_spike = (iv - hist_iv > 0.10) or (hist_iv > 0 and iv / hist_iv > 1.25)
    if is_vol_spike:
        price = price * 0.97
        logs.append(
            f"📊 IV 暴噴警報 (現 IV {iv*100:.1f}% vs 歷史 {hist_iv*100:.1f}%)：預期波動劇烈放大，價格向下修正 3% (撤單重掛更深) 至 ${price:.2f}，以防被恐慌砸盤擊穿。"
        )
    # IV 崩塌工作流 (Vol Crush Workflow)
    elif iv < hist_iv * 0.85:
        price = em_lower
        logs.append(
            f"📊 IV 崩塌警報 (現 IV {iv*100:.1f}% vs 歷史 {hist_iv*100:.1f}%)：預期波動收窄，價格調整至預期區間下限邊緣 ${price:.2f}。"
        )

    # 3. 技術與流動性結構面 (Technical & Liquidity Anchors)
    # 前日收盤價與缺口回補
    if prev_close > 0 and spot_price > 0:
        gap_pct = abs(spot_price - prev_close) / prev_close
        if gap_pct > 0.02:
            price = prev_close
            logs.append(
                f"🛠️ 跳空缺口偵測 (跳空 {gap_pct*100:.1f}%)：錨定在前收盤價 ${prev_close:.2f} 處，捕捉回補缺口的影線。"
            )

    # 整數心理鐵壁與市場深度 (Level 2)
    # 檢查是否有整數心理大關 ($50, $100, $200 等) 在上方 1.5 美元內
    round_levels = [50.0, 100.0, 150.0, 200.0, 250.0, 300.0, 400.0, 500.0]
    for lvl in round_levels:
        if lvl <= price <= lvl + 1.5:
            price = lvl - 0.75
            logs.append(
                f"🛠️ 整數心理大關防禦 (接近 ${lvl:.0f})：調整掛單價格至支撐關卡下方 ${price:.2f}，以精準捕捉散戶停損引發的超跌血水。"
            )
            break

    # 計算最終股數，確保數量不小於 1
    final_quantity = max(
        1.0 if isinstance(base_quantity, float) else 1,
        base_quantity * sizing_multiplier,
    )
    if isinstance(base_quantity, int):
        final_quantity = int(final_quantity)

    return round(price, 2), final_quantity, logs
