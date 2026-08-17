"""WTI 油價警報用戶閾值配置 CRUD。

使用 kv_cache 存儲每個用戶的 WTI 閾值設定：
- Key: wti_config_{user_id}
- Value: JSON-serialized WtiAlertConfig
"""

import logging
from typing import Optional
from pydantic import BaseModel, Field, field_validator

from database.cache import save_kv_cache, get_kv_cache

logger = logging.getLogger(__name__)


class WtiAlertConfig(BaseModel):
    """用戶 WTI 油價閾值配置模型。"""

    upper_price: Optional[float] = Field(
        default=95.0,
        description="WTI 價格上限 (觸發看多/通膨警報)",
        ge=20.0,
        le=250.0,
    )
    lower_price: Optional[float] = Field(
        default=65.0,
        description="WTI 價格下限 (觸發看空/衰退警報)",
        ge=10.0,
        le=200.0,
    )
    pct_change_threshold: float = Field(
        default=3.0,
        description="30 分鐘波動百分比閾值",
        ge=0.5,
        le=20.0,
    )

    @field_validator("upper_price", "lower_price", mode="before")
    @classmethod
    def round_price(cls, v: Optional[float]) -> Optional[float]:
        return round(float(v), 2) if v is not None and str(v).strip() != "" else None

    @field_validator("pct_change_threshold", mode="before")
    @classmethod
    def round_pct(cls, v: float) -> float:
        return round(float(v), 2)


async def get_wti_config(user_id: int) -> WtiAlertConfig:
    """讀取用戶 WTI 閾值配置，不存在或解析失敗則返回預設值。"""
    raw = get_kv_cache(f"wti_config_{user_id}")
    if raw and isinstance(raw, dict):
        try:
            return WtiAlertConfig(**raw)
        except Exception as e:
            logger.warning(f"WTI config 解析失敗 (uid: {user_id}): {e}，使用預設值")
    return WtiAlertConfig()


async def save_wti_config(user_id: int, config: WtiAlertConfig) -> bool:
    """儲存用戶 WTI 閾值配置。"""
    return await save_kv_cache(f"wti_config_{user_id}", config.model_dump())


__all__ = [
    "WtiAlertConfig",
    "get_wti_config",
    "save_wti_config",
]
