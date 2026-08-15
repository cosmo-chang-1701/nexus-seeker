"""NexusEmbed — 全站統一 Discord Embed 子類別。

負責強制執行一致的調色盤、時間戳記與 Footer 排版。
所有子模組都從本模組 import `discord.Embed`（已被 NexusEmbed 替換）。
"""

from typing import Any
import discord

from datetime import datetime, timezone


# 保存原始 Embed 參照，NexusEmbed.from_dict 內部需要用原生版本解析 dict。
_OriginalEmbed = discord.Embed


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
            if color == discord.Color.blue():
                kwargs["color"] = discord.Color(0x3498DB)
            elif color == discord.Color.red() or color == discord.Color.dark_red():
                kwargs["color"] = discord.Color(0xE74C3C)
            elif color == discord.Color.green():
                kwargs["color"] = discord.Color(0x2ECC71)
            elif color == discord.Color.orange():
                kwargs["color"] = discord.Color(0xF39C12)
            elif color == discord.Color.blurple():
                kwargs["color"] = discord.Color(0x5865F2)
        else:
            kwargs["color"] = discord.Color(0x3498DB)

        super().__init__(*args, **kwargs)

        # 2. 確保時間戳記一致存在
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)

    @property
    def color(self) -> Any:
        return super().color

    @color.setter
    def color(self, value: Any) -> Any:
        if value is not None:
            if value == discord.Color.blue():
                value = discord.Color(0x3498DB)
            elif value == discord.Color.red() or value == discord.Color.dark_red():
                value = discord.Color(0xE74C3C)
            elif value == discord.Color.green():
                value = discord.Color(0x2ECC71)
            elif value == discord.Color.orange():
                value = discord.Color(0xF39C12)
            elif value == discord.Color.blurple():
                value = discord.Color(0x5865F2)
        _OriginalEmbed.color.fset(self, value)  # type: ignore

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

        # 實作單一 Field 邊界防護 (256/1024字元上限)
        fields = list(result.get("fields", []))
        for field in fields:
            if "name" in field:
                field["name"] = _safe_clamp_field_name(field["name"])
            if "value" in field:
                field["value"] = _safe_clamp_field_value(field["value"])

        # 實作字數截斷防護 (5800字元上限)
        total_len = len(result.get("title") or "") + len(
            result.get("description") or ""
        )
        if "footer" in result and "text" in result["footer"]:
            total_len += len(result["footer"]["text"])
        if "author" in result and "name" in result["author"]:
            total_len += len(result["author"]["name"])

        for field in fields:
            total_len += len(field.get("name") or "") + len(field.get("value") or "")

        if total_len > 5800:
            warning = "⚠️ (因自選標的過多，已啟用自動截斷防護，僅保留核心數據)"
            while total_len > 5800 and fields:
                field = fields.pop()
                total_len -= len(field.get("name") or "") + len(
                    field.get("value") or ""
                )
            result["fields"] = fields

            desc = result.get("description") or ""
            if warning not in desc:
                result["description"] = f"{desc}\n\n{warning}" if desc else warning
        else:
            result["fields"] = fields

        return result


def install_nexus_embed() -> None:
    """將 discord.Embed 替換為 NexusEmbed，攔截全站 Embed 建立。

    只需在 embed_builder 模組層級呼叫一次。子模組不需要重複呼叫。
    """
    discord.Embed = NexusEmbed  # type: ignore[misc]
