"""產業鏈交叉驗證因果檢驗與高頻臨近預測 (Channel Check Decision Engine)。

實作兩種分流檢驗機制：
1. `CAUSAL` (因果傳導鏈):
   上游資本支出/訂單積壓傳導至下游營收，計算傳導背離度點數 (divergence_pp)，
   判定 CONFIRM / DIVERGE / INSUFFICIENT。
2. `NOWCAST` (高頻臨近預測):
   以實體客流、鐵路裝載或台廠供應鏈月營收高頻指標，檢驗方向性命中率 (nowcast_hit)
   與滾動相關係數 (correlation)。

傳導極性：`SupplyChainLink.polarity == -1`（反向關係，例如零售 DIO 上升壓制品牌廠出貨）時，
判定前先把驅動端增長率乘上極性；`driver_growth` 欄位仍保存原始量測值。

時間對齊：兩端皆為同一曆季（`as_of_period`）的觀測值；`lead_lag_quarters` 僅為說明，
尚未實作領先落後位移。
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any

from market_analysis.fundamental_pipeline.models import (
    ChannelCheckLogRecord,
    ChannelCheckResult,
    ChannelCheckVerdict,
    NowcastDirection,
    SupplyChainLink,
)
from market_analysis.fundamental_pipeline.supply_chain_map import (
    extract_symbols_from_link,
)

# 使用者可見之繁中對照（/fa 與日誌摘要一律使用中文，不顯示英文狀態碼）
VERDICT_LABELS_ZH: dict[str, str] = {
    "CONFIRM": "共振確認",
    "DIVERGE": "背離",
    "INSUFFICIENT": "資料不足",
}
LINK_TYPE_LABELS_ZH: dict[str, str] = {
    "CAUSAL": "因果傳導",
    "NOWCAST": "高頻臨近預測",
}
NOWCAST_DIRECTION_LABELS_ZH: dict[str, str] = {
    "NOWCAST_UP": "預測向上",
    "NOWCAST_DOWN": "預測向下",
    "FLAT": "持平",
}

# 關鍵量化門檻常數
DEFAULT_CAUSAL_DIVERGENCE_THRESHOLD_PP: float = 25.0
DEFAULT_NOWCAST_DIRECTION_THRESHOLD_PCT: float = 2.0
MIN_CORRELATION_SAMPLE_SIZE: int = 4


def compute_pearson_correlation(
    series_x: Sequence[float | None], series_y: Sequence[float | None]
) -> float | None:
    """計算兩組數列之皮爾森積差相關係數 (自由度 N >= 4)。"""
    if len(series_x) != len(series_y) or len(series_x) < MIN_CORRELATION_SAMPLE_SIZE:
        return None

    # 過濾包含 None, NaN 或 Inf 的異常值
    cleaned: list[tuple[float, float]] = []
    for x, y in zip(series_x, series_y):
        if x is None or y is None:
            continue
        try:
            x_f = float(x)
            y_f = float(y)
        except (ValueError, TypeError):
            continue
        if math.isnan(x_f) or math.isinf(x_f) or math.isnan(y_f) or math.isinf(y_f):
            continue
        cleaned.append((x_f, y_f))

    n = len(cleaned)
    if n < MIN_CORRELATION_SAMPLE_SIZE:
        return None

    mean_x = sum(pt[0] for pt in cleaned) / n
    mean_y = sum(pt[1] for pt in cleaned) / n

    cov_xy = sum((pt[0] - mean_x) * (pt[1] - mean_y) for pt in cleaned)
    var_x = sum((pt[0] - mean_x) ** 2 for pt in cleaned)
    var_y = sum((pt[1] - mean_y) ** 2 for pt in cleaned)

    denom = math.sqrt(var_x * var_y)
    if denom < 1e-9:
        return 0.0

    corr = cov_xy / denom
    # 物理數值箝制在 [-1.0, +1.0]
    return max(-1.0, min(1.0, round(corr, 3)))


def _with_note(text: str, note: str) -> str:
    return f"{text}：{note}" if note else text


def evaluate_causal_link(
    link: SupplyChainLink,
    as_of_period: str,
    driver_growth: float | None,
    follower_growth: float | None,
    divergence_threshold_pp: float = DEFAULT_CAUSAL_DIVERGENCE_THRESHOLD_PP,
    members_data: dict[str, Any] | None = None,
    data_note: str = "",
) -> ChannelCheckResult:
    """評估 CAUSAL 因果傳導鏈之共振與背離狀態（驅動端先乘上 polarity）。"""
    members = members_data if members_data is not None else {}
    members_dict = {
        "link_key": link.link_key,
        "title": link.title,
        "pillar": link.pillar,
        "lead_lag_quarters": link.lead_lag_quarters,
        "polarity": link.polarity,
        "description": link.description,
        "drivers": link.drivers,
        "followers": link.followers,
        "symbols": extract_symbols_from_link(link),
        **members,
    }

    # 1. 檢驗數據完整性
    if driver_growth is None or follower_growth is None:
        return ChannelCheckResult(
            link_key=link.link_key,
            title=link.title,
            link_type=link.link_type,
            experimental=link.experimental,
            as_of_period=as_of_period,
            driver_growth=driver_growth,
            follower_growth=follower_growth,
            divergence_pp=None,
            nowcast_direction=None,
            nowcast_hit=None,
            correlation=None,
            verdict="INSUFFICIENT",
            summary_text=_with_note("數據不充分 (驅動端或跟隨端增長率缺失)", data_note),
            members=members_dict,
        )

    # 2. 套用傳導極性後計算背離度點數 (follower - polarity × driver)
    raw_driver = driver_growth
    driver_growth = round(raw_driver * link.polarity, 2)
    polarity_note = (
        f"｜ 反向關係：驅動原值 {raw_driver:+.1f}% 取負號比較 "
        if link.polarity == -1
        else ""
    )
    divergence_pp = round(follower_growth - driver_growth, 2)
    abs_div = abs(divergence_pp)

    # 3. 判定共振與背離
    # 同向擴張 (兩者皆正) 或 同向收縮 (兩者皆負)
    if (driver_growth >= 0 and follower_growth >= 0) or (
        driver_growth < 0 and follower_growth < 0
    ):
        if abs_div <= divergence_threshold_pp:
            verdict: ChannelCheckVerdict = "CONFIRM"
            if driver_growth >= 0:
                summary_text = (
                    f"因果傳導擴張共振確認 (驅動增長 {driver_growth:+.1f}% ｜ "
                    f"跟隨增長 {follower_growth:+.1f}% ｜ 偏差 {divergence_pp:+.1f}pp)"
                )
            else:
                summary_text = (
                    f"因果傳導同步收縮確認 (驅動增長 {driver_growth:+.1f}% ｜ "
                    f"跟隨增長 {follower_growth:+.1f}% ｜ 偏差 {divergence_pp:+.1f}pp)"
                )
        else:
            verdict = "DIVERGE"
            summary_text = (
                f"因果傳導幅度顯著背離 (驅動增長 {driver_growth:+.1f}% ｜ "
                f"跟隨增長 {follower_growth:+.1f}% ｜ 偏差 {divergence_pp:+.1f}pp > 門檻 {divergence_threshold_pp:.1f}pp)"
            )
    else:
        # 反向走向
        if abs_div <= 5.0:
            # 零軸微幅波動邊界 (例如 +1% vs -1%)
            verdict = "CONFIRM"
            summary_text = (
                f"兩端微幅震盪近乎持平 (驅動 {driver_growth:+.1f}% ｜ "
                f"跟隨 {follower_growth:+.1f}% ｜ 偏差 {divergence_pp:+.1f}pp)"
            )
        else:
            verdict = "DIVERGE"
            summary_text = (
                f"因果傳導方向完全背離 (驅動增長 {driver_growth:+.1f}% ｜ "
                f"跟隨增長 {follower_growth:+.1f}% ｜ 偏差 {divergence_pp:+.1f}pp)"
            )

    if polarity_note:
        summary_text = f"{summary_text} {polarity_note.strip()}"

    return ChannelCheckResult(
        link_key=link.link_key,
        title=link.title,
        link_type=link.link_type,
        experimental=link.experimental,
        as_of_period=as_of_period,
        driver_growth=raw_driver,
        follower_growth=follower_growth,
        divergence_pp=divergence_pp,
        nowcast_direction=None,
        nowcast_hit=None,
        correlation=None,
        verdict=verdict,
        summary_text=summary_text,
        members=members_dict,
    )


def evaluate_nowcast_link(
    link: SupplyChainLink,
    as_of_period: str,
    driver_growth: float | None,
    follower_growth: float | None = None,
    driver_history: Sequence[float | None] | None = None,
    follower_history: Sequence[float | None] | None = None,
    direction_threshold_pct: float = DEFAULT_NOWCAST_DIRECTION_THRESHOLD_PCT,
    allow_nowcast_preview: bool = True,
    members_data: dict[str, Any] | None = None,
    data_note: str = "",
) -> ChannelCheckResult:
    """評估 NOWCAST 高頻臨近預測之方向性命中率與相關性檢驗（先行指標先乘上 polarity）。"""
    members = members_data if members_data is not None else {}
    members_dict = {
        "link_key": link.link_key,
        "title": link.title,
        "pillar": link.pillar,
        "lead_lag_quarters": link.lead_lag_quarters,
        "polarity": link.polarity,
        "description": link.description,
        "drivers": link.drivers,
        "followers": link.followers,
        "symbols": extract_symbols_from_link(link),
        **members,
    }

    # 1. 檢驗驅動端高頻數據
    if driver_growth is None:
        return ChannelCheckResult(
            link_key=link.link_key,
            title=link.title,
            link_type=link.link_type,
            experimental=link.experimental,
            as_of_period=as_of_period,
            driver_growth=None,
            follower_growth=follower_growth,
            divergence_pp=None,
            nowcast_direction=None,
            nowcast_hit=None,
            correlation=None,
            verdict="INSUFFICIENT",
            summary_text=_with_note("數據不充分 (高頻先行指標數據缺失)", data_note),
            members=members_dict,
        )

    raw_driver = driver_growth
    driver_growth = round(raw_driver * link.polarity, 2)

    # 2. 判定高頻指標方向性
    if driver_growth >= direction_threshold_pct:
        direction: NowcastDirection = "NOWCAST_UP"
    elif driver_growth <= -direction_threshold_pct:
        direction = "NOWCAST_DOWN"
    else:
        direction = "FLAT"

    # 3. 計算歷史相關係數 (若有提供序列)
    correlation: float | None = None
    if driver_history is not None and follower_history is not None:
        correlation = compute_pearson_correlation(driver_history, follower_history)

    # 4. 判定臨近預測命中率與結果
    nowcast_hit: bool | None = None
    divergence_pp: float | None = None

    if follower_growth is not None:
        divergence_pp = round(follower_growth - driver_growth, 2)
        if direction == "NOWCAST_UP":
            nowcast_hit = follower_growth > 0.0
        elif direction == "NOWCAST_DOWN":
            nowcast_hit = follower_growth < 0.0
        else:
            nowcast_hit = abs(follower_growth) <= direction_threshold_pct

        if nowcast_hit:
            verdict: ChannelCheckVerdict = "CONFIRM"
            summary_text = (
                f"高頻臨近預測方向命中 (先行指標 {driver_growth:+.1f}% ｜ "
                f"跟隨實績 {follower_growth:+.1f}%)"
            )
        else:
            verdict = "DIVERGE"
            summary_text = (
                f"高頻臨近預測方向背離 (先行指標 {driver_growth:+.1f}% ｜ "
                f"跟隨實績 {follower_growth:+.1f}%)"
            )
    else:
        # 跟隨端尚未發布 (在途預測)
        if allow_nowcast_preview:
            if direction == "NOWCAST_UP":
                verdict = "CONFIRM"
                summary_text = f"高頻先行指標偏多擴張 (先行增長 {driver_growth:+.1f}% ｜ 待跟隨端財報發布)"
            elif direction == "FLAT":
                verdict = "CONFIRM"
                summary_text = f"高頻先行指標持平中性 (先行增長 {driver_growth:+.1f}% ｜ 待跟隨端財報發布)"
            else:
                verdict = "DIVERGE"
                summary_text = f"高頻先行指標偏空示警 (先行衰退 {driver_growth:+.1f}% ｜ 待跟隨端財報發布)"
        else:
            verdict = "INSUFFICIENT"
            summary_text = _with_note("待跟隨端季度財報公布驗證", data_note)

    return ChannelCheckResult(
        link_key=link.link_key,
        title=link.title,
        link_type=link.link_type,
        experimental=link.experimental,
        as_of_period=as_of_period,
        driver_growth=raw_driver,
        follower_growth=follower_growth,
        divergence_pp=divergence_pp,
        nowcast_direction=direction,
        nowcast_hit=nowcast_hit,
        correlation=correlation,
        verdict=verdict,
        summary_text=summary_text,
        members=members_dict,
    )


def evaluate_channel_check(
    link: SupplyChainLink,
    as_of_period: str,
    driver_growth: float | None,
    follower_growth: float | None = None,
    driver_history: Sequence[float | None] | None = None,
    follower_history: Sequence[float | None] | None = None,
    divergence_threshold_pp: float = DEFAULT_CAUSAL_DIVERGENCE_THRESHOLD_PP,
    direction_threshold_pct: float = DEFAULT_NOWCAST_DIRECTION_THRESHOLD_PCT,
    allow_nowcast_preview: bool = True,
    members_data: dict[str, Any] | None = None,
    data_note: str = "",
) -> ChannelCheckResult:
    """產業鏈交叉驗證統一決策入口。"""
    if link.link_type == "CAUSAL":
        return evaluate_causal_link(
            link=link,
            as_of_period=as_of_period,
            driver_growth=driver_growth,
            follower_growth=follower_growth,
            divergence_threshold_pp=divergence_threshold_pp,
            members_data=members_data,
            data_note=data_note,
        )
    return evaluate_nowcast_link(
        link=link,
        as_of_period=as_of_period,
        driver_growth=driver_growth,
        follower_growth=follower_growth,
        driver_history=driver_history,
        follower_history=follower_history,
        direction_threshold_pct=direction_threshold_pct,
        allow_nowcast_preview=allow_nowcast_preview,
        members_data=members_data,
        data_note=data_note,
    )


def result_to_log_record(result: ChannelCheckResult) -> ChannelCheckLogRecord:
    """將 ChannelCheckResult 轉換為可直接寫入資料庫之 ChannelCheckLogRecord。"""
    return ChannelCheckLogRecord(
        link_key=result.link_key,
        as_of_period=result.as_of_period,
        link_type=result.link_type,
        experimental=result.experimental,
        driver_growth=result.driver_growth,
        follower_growth=result.follower_growth,
        divergence_pp=result.divergence_pp,
        nowcast_direction=result.nowcast_direction,
        nowcast_hit=result.nowcast_hit,
        correlation=result.correlation,
        verdict=result.verdict,
        # summary_text 一併存入 members_json，/fa 才能顯示「資料不足」的原因
        members_json=json.dumps(
            {**result.members, "summary_text": result.summary_text},
            ensure_ascii=False,
        ),
    )
