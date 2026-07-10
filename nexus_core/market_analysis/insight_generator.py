from typing import Dict, Any


def compute_realtime_insights(data: Dict[str, Any]) -> str:
    sym = data.get("symbol", "UNKNOWN")
    spot = data.get("spot", 0.0)
    max_pain = data.get("max_pain", 0.0)
    put_wall = data.get("put_wall", 0.0)
    gex_status = data.get("gex_status", "UNKNOWN")

    # Optional fields for deeper analysis if present
    uoa_calls_vol = data.get("uoa_calls_vol", 0.0)
    uoa_puts_vol = data.get("uoa_puts_vol", 0.0)
    skew_percentile = data.get("skew_percentile", 50.0)

    if max_pain > 0 and spot > 0:
        dist_pct = (max_pain - spot) / spot * 100
    else:
        dist_pct = 0.0

    # RiskContext: 底牆危機判斷
    is_put_wall_crisis = False
    if put_wall > 0 and spot > 0:
        distance_to_pw = (spot - put_wall) / spot
        if distance_to_pw <= 0.02 and "NEG" in str(gex_status).upper():
            is_put_wall_crisis = True

    if is_put_wall_crisis:
        max_pain_diff = round(abs(dist_pct), 1)
        return f"• ⚠️ **{sym}**: 雖然股價遠低於 Max Pain ({max_pain_diff}%)，但因現價已穿透/逼近做市商 PutWall 防線，進入負 Gamma 禁區。做市商 Delta 剛性拋壓（Delta Negative Feedback Loop）已全面主導盤面。此處散戶搶購 Call 的偏斜亢奮將加劇做市商的對沖拋售壓力，嚴禁任何左側接刀行為，靜待底牆企穩。"

    # 1. 籌碼狀態
    chip_status = "⚖️ 籌碼結構與情緒波動相對均衡"
    if (
        uoa_calls_vol > uoa_puts_vol * 1.5
        and uoa_calls_vol > 0
        and skew_percentile <= 30.0
    ):
        chip_status = "🔥 籌碼面呈大額買權掃貨且看漲情緒亢奮"
    elif (
        uoa_puts_vol > uoa_calls_vol * 1.5
        and uoa_puts_vol > 0
        and skew_percentile >= 70.0
    ):
        chip_status = "⚠️ 籌碼面顯現大額避險 Put 流入且下行保護需求高企"
    elif uoa_calls_vol > uoa_puts_vol * 1.5 and uoa_calls_vol > 0:
        chip_status = "📈 籌碼面出現大額 Call 掃單"
    elif uoa_puts_vol > uoa_calls_vol * 1.5 and uoa_puts_vol > 0:
        chip_status = "📉 籌碼面湧現大額 Put 避險買盤"
    elif skew_percentile >= 70.0:
        chip_status = "🛡️ 情緒面呈現 Put 偏斜昂貴，避險情緒高溫"
    elif skew_percentile <= 30.0:
        chip_status = "🚀 情緒面呈現 Call 偏斜亢奮，散戶搶購看漲"

    # 2. 量化偏離事實
    if dist_pct > 10.0:
        dev_fact = f"，股價低於 Max Pain {abs(dist_pct):.1f}%，存在顯著的向上磁吸效應"
    else:
        dev_fact = (
            f"，股價高於 Max Pain {abs(dist_pct):.1f}%，面臨向上動能衰退與拉回修正壓力"
        )

    # 3. 實盤防禦指引
    iv_rank = data.get("iv_rank", 50.0)

    if iv_rank < 15.0:
        if dist_pct > 0:
            guidance = "；IVR 處於絕對低位，具備高盈虧比的買方建倉條件。建議透過買入看漲期權 (Long Call) 或構建借方價差 (Debit Spread) 捕捉磁吸效應，避免 Vega 擴張風險。"
        else:
            guidance = "；IVR 處於絕對低位，建議以買入看跌期權 (Long Put) 或借方價差 (Debit Spread) 防禦，避免賣出期權的 Vega 擴張風險。"
    else:
        guidance = "；操作上建議於支撐區間分批逢低吸納，降低建倉成本。"
        if dist_pct < -10.0 and skew_percentile >= 70.0:
            guidance = "；操作上應嚴禁單腿追高，建議以現貨持有搭配賣出 OTM Call（備兌看漲）進行防禦保護，或買入 Put 鎖定下行風險。"
        elif dist_pct < -10.0 and skew_percentile <= 30.0:
            guidance = "；此時散戶搶購情緒極端，防範主力拉回殺多，嚴禁盲目單腿裸買 Call，建議逢高分批鎖定利潤。"
        elif dist_pct > 10.0 and uoa_calls_vol > uoa_puts_vol * 1.5:
            guidance = "；量化磁吸與多頭籌碼共振，可於波動下緣分批逢低吸納，或部署 Bull Put Spread 策略。"
        elif dist_pct > 10.0 and skew_percentile >= 70.0:
            guidance = "；雖有磁吸引力但避險情緒仍重，建議透過賣出 CSP（備兌看跌）分批建倉，避免單腿買入。"
        elif dist_pct < -10.0:
            guidance = "；操作上建議保持現貨防禦，嚴禁單腿操作。"

    emoji, text = chip_status.split(" ", 1)
    return f"• {emoji} **{sym}**: {text}{dev_fact}{guidance}"
