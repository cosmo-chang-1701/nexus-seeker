"""NexusEmbed — 全站統一 Discord Embed 子類別。

負責強制執行一致的調色盤、時間戳記與 Footer 排版。
所有子模組皆應改用 `NexusEmbed` 取代 `discord.Embed` 來建立 Embed 物件。
"""

from typing import Any, Optional
import discord

from datetime import datetime, timezone

from ui.panel_renderer import truncate_with_boundary


# 保存原始 Embed 參照，NexusEmbed.from_dict 內部需要用原生版本解析 dict。
_OriginalEmbed = discord.Embed

# Discord API 硬限制
_MAX_FIELD_COUNT = 25
_MAX_DESCRIPTION_LEN = 4096
_MAX_TOTAL_LEN = 5800  # 保守值，低於官方 6000 字元總長上限

_OVERFLOW_DESC_WARNING = "…（內容過長已自動截斷）"


# ============================================================================
# 統一語意調色盤 (Curated Semantic Palette)
#
# 每個語意類別對應唯一一個精選色值；所有 discord 原生 Color 工廠函式都會被
# remap 到這張表，確保「成功／失敗／警示／資訊／防禦／管理／特殊事件／中性」
# 這些語意在全站的呈現顏色永遠一致。
# ============================================================================
_COLOR_SUCCESS = 0x2ECC71  # 成功 / 多頭 / 安全
_COLOR_ERROR = 0xE74C3C  # 危險 / 錯誤 / 空頭 / 否決
_COLOR_WARNING = 0xF39C12  # 警示 / 待關注 / 中性偏警戒
_COLOR_INFO = 0x3498DB  # 一般資訊 / 中性
_COLOR_BRAND = 0x5865F2  # Nexus 品牌 / 系統框架
_COLOR_DEFENSE = 0x1ABC9C  # 對沖 / 防禦性倉位
_COLOR_ADMIN = 0x9B59B6  # 設定 / 管理介面
_COLOR_SPECIAL = 0x8E44AD  # 罕見市場事件 (擠壓、巨鯨共振、尾部風險)
_COLOR_NEUTRAL = 0x99AAB5  # 無數據 / 未定義 / 中性

# 原生 discord.Color 工廠函式 → 語意調色盤的映射表。
# 使用 lambda 延後呼叫，避免模組載入時就建立 discord.Color 實例。
_COLOR_REMAP: list[tuple[Any, int]] = [
    (lambda: discord.Color.green(), _COLOR_SUCCESS),
    (lambda: discord.Color.red(), _COLOR_ERROR),
    (lambda: discord.Color.dark_red(), _COLOR_ERROR),
    (lambda: discord.Color.orange(), _COLOR_WARNING),
    (lambda: discord.Color.gold(), _COLOR_WARNING),
    (lambda: discord.Color.blue(), _COLOR_INFO),
    (lambda: discord.Color.dark_blue(), _COLOR_INFO),
    (lambda: discord.Color.blurple(), _COLOR_BRAND),
    (lambda: discord.Color.teal(), _COLOR_DEFENSE),
    (lambda: discord.Color.dark_teal(), _COLOR_DEFENSE),
    (lambda: discord.Color.dark_magenta(), _COLOR_ADMIN),
    (lambda: discord.Color.purple(), _COLOR_SPECIAL),
    (lambda: discord.Color.dark_grey(), _COLOR_NEUTRAL),
    (lambda: discord.Color.default(), _COLOR_NEUTRAL),
]


def _remap_color(value: Any) -> Any:
    """將任何原生 discord.Color 語意色，remap 到統一的精選調色盤。"""
    if value is None:
        return None
    for factory, curated_hex in _COLOR_REMAP:
        if value == factory():
            return discord.Color(curated_hex)
    return value


def _safe_clamp_field_value(value: Any, max_len: int = 1024) -> str:
    """確保 Field Value 不超過 1024 字元，若為 codeblock 則保持安全閉合。"""
    val_str = str(value) if value is not None else ""
    if len(val_str) <= max_len:
        return val_str
    if val_str.startswith("```") and val_str.rstrip().endswith("```"):
        first_line = val_str.split("\n", 1)[0]
        fence_end = "\n```"
        max_inner = max_len - len(first_line) - 1 - len(fence_end)
        if max_inner > 0:
            inner = val_str[len(first_line) + 1 : -len(fence_end)]
            return f"{first_line}\n{inner[:max_inner]}{fence_end}"
    return val_str[: max_len - 3] + "..."


def _safe_clamp_field_name(name: Any, max_len: int = 256) -> str:
    """確保 Field Name 不超過 256 字元。"""
    name_str = str(name) if name is not None else ""
    if len(name_str) <= max_len:
        return name_str
    return name_str[: max_len - 3] + "..."


class NexusEmbed(discord.Embed):
    """自訂 Embed 子類別，用以動態實現一致的版面設計、精緻調色盤與標準 Footer 排版。"""

    def __init__(self, *args, **kwargs):  # type: ignore
        # 1. 統一對齊和諧且精美的高級調色盤 (Curated Aesthetic Palette)
        color = kwargs.get("color")
        if color is not None:
            kwargs["color"] = _remap_color(color)
        else:
            kwargs["color"] = discord.Color(_COLOR_INFO)

        super().__init__(*args, **kwargs)

        # 2. 確保時間戳記一致存在
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)

    @property
    def color(self) -> Any:
        return super().color

    @color.setter
    def color(self, value: Any) -> Any:
        _OriginalEmbed.color.fset(self, _remap_color(value))  # type: ignore

    @property
    def colour(self) -> Any:
        return self.color

    @colour.setter
    def colour(self, value: Any) -> Any:
        self.color = value

    def set_footer(
        self, *, text: str | None = None, icon_url: str | None = None
    ) -> Any:
        if text:
            # 3. 統一版面 Footer 排版 signature
            prefix = "🌌 Nexus Seeker • "
            clean_text = text
            for p in (
                "🌌 Nexus Seeker • ",
                "Nexus Seeker • ",
                "Nexus Seeker | ",
                "Nexus Seeker ",
            ):
                if clean_text.startswith(p):
                    clean_text = clean_text[len(p) :]
            text = f"{prefix}{clean_text}"
        super().set_footer(text=text, icon_url=icon_url)

    def add_field(self, *, name: Any, value: Any, inline: bool = True) -> "NexusEmbed":
        safe_name = _safe_clamp_field_name(name)
        safe_value = _safe_clamp_field_value(value)
        super().add_field(name=safe_name, value=safe_value, inline=inline)
        return self

    def insert_field_at(
        self, index: int, *, name: Any, value: Any, inline: bool = True
    ) -> "NexusEmbed":
        safe_name = _safe_clamp_field_name(name)
        safe_value = _safe_clamp_field_value(value)
        super().insert_field_at(index, name=safe_name, value=safe_value, inline=inline)
        return self

    def set_field_at(
        self, index: int, *, name: Any, value: Any, inline: bool = True
    ) -> "NexusEmbed":
        safe_name = _safe_clamp_field_name(name)
        safe_value = _safe_clamp_field_value(value)
        super().set_field_at(index, name=safe_name, value=safe_value, inline=inline)
        return self

    @classmethod
    def from_dict(cls, data: Any):  # type: ignore
        embed = _OriginalEmbed.from_dict(data)
        nexus_embed = cls(
            title=embed.title,
            description=embed.description,
            color=embed.color,
            timestamp=embed.timestamp,
            url=embed.url,
        )
        if embed.footer:
            nexus_embed.set_footer(
                text=embed.footer.text, icon_url=embed.footer.icon_url
            )
        if embed.image:
            nexus_embed.set_image(url=embed.image.url)
        if embed.thumbnail:
            nexus_embed.set_thumbnail(url=embed.thumbnail.url)
        if embed.author:
            nexus_embed.set_author(
                name=embed.author.name,
                url=embed.author.url,
                icon_url=embed.author.icon_url,
            )
        for field in embed.fields:
            nexus_embed.add_field(
                name=field.name, value=field.value, inline=field.inline
            )
        return nexus_embed

    def to_dict(self) -> Any:
        result = super().to_dict()

        # 0. 保底 Footer：即使呼叫端忘記呼叫 set_footer()，序列化時仍強制附上品牌 Footer。
        footer = result.get("footer")
        if not footer or not footer.get("text"):
            result["footer"] = {"text": "🌌 Nexus Seeker"}

        # 1. 獨立防護 Description 上限 (4096 字元)，避免長描述、少欄位時漏檢。
        description = result.get("description")
        if description and len(description) > _MAX_DESCRIPTION_LEN:
            room = _MAX_DESCRIPTION_LEN - len(_OVERFLOW_DESC_WARNING)
            result["description"] = (
                truncate_with_boundary(description, room) + _OVERFLOW_DESC_WARNING
            )

        # 2. 實作單一 Field 邊界防護 (256/1024字元上限)
        fields = list(result.get("fields", []))
        for field in fields:
            if "name" in field:
                field["name"] = _safe_clamp_field_name(field["name"])
            if "value" in field:
                field["value"] = _safe_clamp_field_value(field["value"])

        # 3. 欄位數量防護 (25 個上限)：超過時捨棄尾端欄位。
        overflowed_by_count = len(fields) > _MAX_FIELD_COUNT
        if overflowed_by_count:
            fields = fields[:_MAX_FIELD_COUNT]

        # 4. 實作字數截斷防護 (5800字元總長上限)
        total_len = len(result.get("title") or "") + len(
            result.get("description") or ""
        )
        if "footer" in result and "text" in result["footer"]:
            total_len += len(result["footer"]["text"])
        if "author" in result and "name" in result["author"]:
            total_len += len(result["author"]["name"])

        for field in fields:
            total_len += len(field.get("name") or "") + len(field.get("value") or "")

        overflowed_by_length = total_len > _MAX_TOTAL_LEN
        if overflowed_by_length:
            while total_len > _MAX_TOTAL_LEN and fields:
                field = fields.pop()
                total_len -= len(field.get("name") or "") + len(
                    field.get("value") or ""
                )

        result["fields"] = fields

        return result


# ============================================================================
# 期權資料新鮮度標示共用 Helper
#
# 供各 embed_builders 子模組共用的快取新鮮度／降級／時間戳後綴產生函式，
# 統一全站對「資料是否即時」的視覺語彙，避免各模組各自寫死不一致的字串。
# ============================================================================


def format_gex_stale_suffix(is_stale_cache: bool) -> str:
    """GEX 快取降級標記後綴，供讀取 `_is_stale_cache` 旗標的 embed builder 共用。"""
    return " [快取 / API 降級]" if is_stale_cache else ""


def format_market_cache_freshness_suffix(
    is_stale: bool = False,
    is_degraded: bool = False,
    calculation_mode: str = "OI",
    circuit_breaker_triggered: bool = False,
) -> str:
    """market_cache 表 (Max Pain) 新鮮度/降級標記後綴。優先序：斷路器 > OI 降級 > 快取過期。"""
    if circuit_breaker_triggered:
        return " (已觸發斷路器)"
    if is_degraded or calculation_mode == "Volume":
        return " (Volume 降級)"
    if is_stale:
        return " [快取 / API 降級]"
    return ""


def format_cache_age_suffix(
    age_seconds: Optional[float], stale_threshold_seconds: Optional[float] = None
) -> str:
    """依快取資料年齡回傳人類可讀時間戳後綴，讓使用者能看到資料實際的日期時間，
    而不只是一個「是否降級」的布林標記。age_seconds 為 None 時回傳空字串
    （無法判斷年齡，避免誤導使用者資料是新鮮的）。若提供 stale_threshold_seconds
    且已超過門檻，額外附上降級標記。"""
    if age_seconds is None or age_seconds < 0:
        return ""
    minutes = int(age_seconds // 60)
    if minutes < 1:
        age_str = "剛剛"
    elif minutes < 60:
        age_str = f"{minutes}分鐘前"
    else:
        age_str = f"{minutes // 60}小時前"
    if stale_threshold_seconds is not None and age_seconds >= stale_threshold_seconds:
        return f" [快取 / API 降級，更新於{age_str}]"
    return f" (更新於{age_str})"
