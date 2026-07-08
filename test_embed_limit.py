class MockEmbed:
    def __init__(self, **kwargs):
        self.title = kwargs.get("title")
        self.description = kwargs.get("description", "")
        self.color = kwargs.get("color")
        self.timestamp = kwargs.get("timestamp")
        self.url = kwargs.get("url")

        self.footer = None
        self.image = None
        self.thumbnail = None
        self.author = None

        self._fields = []

    @property
    def fields(self):
        return self._fields

    def add_field(self, name, value, inline=False):
        class Field:
            def __init__(self, n, v, i):
                self.name = n
                self.value = v
                self.inline = i

        self._fields.append(Field(name, value, inline))

    def set_footer(self, text=None, icon_url=None):
        class Footer:
            def __init__(self, t, i):
                self.text = t
                self.icon_url = i

        self.footer = Footer(text, icon_url)

    def to_dict(self):
        # 實作字數截斷防護 (5800字元上限)
        total_len = len(self.title or "") + len(self.description or "")
        if self.footer and self.footer.text:
            total_len += len(self.footer.text)
        if self.author and self.author.name:
            total_len += len(self.author.name)
        for field in self.fields:
            total_len += len(field.name or "") + len(field.value or "")

        if total_len > 5800:
            warning = "⚠️ (因自選標的過多，已啟用自動截斷防護，僅保留核心數據)"
            while total_len > 5800 and self._fields:
                field = self._fields.pop()
                total_len -= len(field.name or "") + len(field.value or "")

            if self.description:
                if warning not in self.description:
                    self.description += f"\n\n{warning}"
            else:
                self.description = warning

        d = {}
        if self.title:
            d["title"] = self.title
        if self.description:
            d["description"] = self.description
        if self.footer:
            d["footer"] = {"text": self.footer.text}
        d["fields"] = [{"name": f.name, "value": f.value} for f in self._fields]
        return d


def run_test():
    # 建立一個測試用的 MockEmbed，模擬 /x 指令的大量回傳
    embed = MockEmbed(
        title="🌌 核心 AI 暨持倉量化雷達 (測試)",
        description="這是一個壓力測試用的 Embed，模擬多個標的觸發了 TDPQ 突破共振、SQZ 與 MOM 等功能後，是否會超過 Discord 的字數上限。",
    )

    embed.set_footer(text="Nexus Seeker | 戰術風險管理終端")

    # 模擬大量標的資料寫入 Fields
    # 每個 Field 代表 10 檔標的，每檔標的都有冗長的文字與 ANSI
    for i in range(15):
        ansi_table = "```ansi\n"
        ansi_table += "============================= 核心 AI 暨持倉量化雷達 =============================\n"
        ansi_table += f"{'標的':<8}{'價格 (漲跌)':<16}{'IVR':<8}{'本週預期區間 (EM)':<22}{'Max Pain':<11}{'SQZ':<4}{'MOM':<7}{'與痛點價差 (D-MP)'}\n"
        ansi_table += "-" * 91 + "\n"

        for j in range(5):
            sym = f"SYM{i}{j}"
            ansi_table += f"\u001b[1;34m{sym:<6}\u001b[0m"  # 標的
            ansi_table += "\u001b[1;32m$100.00\u001b[0m (+1.50%)"  # 價格
            ansi_table += "50.0%   "  # IVR
            ansi_table += "±$5.00                "  # EM
            ansi_table += "$105.00    "  # Max Pain
            ansi_table += "🟢  "  # SQZ
            ansi_table += "\u001b[1;32m+2.5\u001b[0m   "  # MOM
            ansi_table += "\u001b[1;31m+5.0%\u001b[0m       "  # D-MP
            ansi_table += "✨ TDP 估值三擊 (Triple Discount Pricing)\n"  # Label

            # 再加入 TDPQ 突破共振的假資料
            sym2 = f"TSQ{i}{j}"
            ansi_table += f"\u001b[1;34m{sym2:<6}\u001b[0m"
            ansi_table += "\u001b[1;32m$120.00\u001b[0m (+3.50%)"
            ansi_table += "80.0%   "
            ansi_table += "±$8.00                "
            ansi_table += "$115.00    "
            ansi_table += "🟢  "
            ansi_table += "\u001b[1;32m+4.5\u001b[0m   "
            ansi_table += "\u001b[1;32m-4.1%\u001b[0m       "
            ansi_table += "⚡ TDPQ 突破共振 (Triple Discount + Squeeze)\n"

        ansi_table += "=================================================================================\n```"

        embed.add_field(name=f"📦 掃描批次 ({i+1})", value=ansi_table, inline=False)

    # 顯示原本的總字數 (繞過 to_dict)
    raw_len = len(embed.title or "") + len(embed.description or "")
    if embed.footer and embed.footer.text:
        raw_len += len(embed.footer.text)
    for field in embed.fields:
        raw_len += len(field.name or "") + len(field.value or "")

    print(f"📊 [測試前] 模擬注入的總字元數 (未經截斷): {raw_len} 字元")

    # 觸發 to_dict() 進行截斷防護
    result_dict = embed.to_dict()

    # 計算截斷後的總字數
    final_len = len(result_dict.get("title", "")) + len(
        result_dict.get("description", "")
    )
    if "footer" in result_dict:
        final_len += len(result_dict["footer"].get("text", ""))
    if "fields" in result_dict:
        for field in result_dict["fields"]:
            final_len += len(field.get("name", "")) + len(field.get("value", ""))

    print(f"🛡️ [測試後] 啟動 NexusEmbed 保護機制後的字數: {final_len} 字元")

    if final_len <= 6000:
        print("✅ 測試通過：總字數成功被壓制在 Discord 的 6000 字元限制以內！")
        if "⚠️ (因自選標的過多，已啟用自動截斷防護" in result_dict.get(
            "description", ""
        ):
            print("✅ 測試通過：已成功於 Description 中附加防護警告文字。")
    else:
        print("❌ 測試失敗：字數仍然超標！")


if __name__ == "__main__":
    run_test()
