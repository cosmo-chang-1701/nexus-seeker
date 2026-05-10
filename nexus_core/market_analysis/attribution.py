import logging
import json
import sqlite3
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
import numpy as np
import config

logger = logging.getLogger(__name__)

class AttributionEngine:
    """
    對沖歸因與自我進化引擎 (Self-Evolving Attribution System)。
    """

    @staticmethod
    def calculate_protection_score(loss_avoided: float, cost_of_hedge: float, event_snapshot: List[dict] = None) -> float:
        """
        計算對沖保護評分 (0-100)。
        Score = (避免的損失 / 對沖成本) * (Polymarket 相關性修正值)。
        """
        if cost_of_hedge <= 0: return 100.0 if loss_avoided > 0 else 0.0
        
        # 基礎效率評分 (以 2x 為 100 分基準)
        base_efficiency = (loss_avoided / cost_of_hedge) * 50
        
        # 相關性修正：若事件快照顯示機率劇烈變動 (Edge)，則給予額外加分
        poly_multiplier = 1.0
        if event_snapshot:
            # 簡化邏輯：若快照中存在機率 > 70% 或 < 30% 的事件，代表有顯著事件驅動
            for event in event_snapshot:
                for option in event.get('odds_distribution', []):
                    odds = option.get('odds', 0.5)
                    if odds > 0.7 or odds < 0.3:
                        poly_multiplier = 1.2 # 機率極端，視為強相關信號
                        break
        
        score = base_efficiency * poly_multiplier
        return float(np.clip(score, 0, 100))

    @staticmethod
    async def log_vtr_hedge(user_id: int, strategy_tag: str, pre_hedge_greeks: dict, poly_snapshot: List[dict] = None):
        """
        [Snapshot Mechanism] 記錄 VTR 對沖執行瞬間的 Greeks 與 Polymarket 機率快照。
        """
        try:
            conn = sqlite3.connect(config.DB_NAME)
            cursor = conn.cursor()
            
            event_context = {
                "poly_event_snapshot": poly_snapshot,
                "timestamp_utc": datetime.now(timezone.utc).isoformat()
            }
            
            cursor.execute("""
                INSERT INTO vtr_hedge_logs (user_id, strategy_tag, event_context, pre_hedge_greeks, status)
                VALUES (?, ?, ?, ?, 'OPEN')
            """, (
                user_id, 
                strategy_tag, 
                json.dumps(event_context, ensure_ascii=False),
                json.dumps(pre_hedge_greeks),
            ))
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to log VTR hedge snapshot: {e}")

    @staticmethod
    async def finalize_vtr_attribution(user_id: int, window_hours: int = 24):
        """
        針對已平倉的 VTR 對沖進行歸因分析。
        """
        try:
            conn = sqlite3.connect(config.DB_NAME)
            cursor = conn.cursor()
            
            cursor.execute("SELECT id, event_context, pre_hedge_greeks FROM vtr_hedge_logs WHERE user_id = ? AND status = 'OPEN'", (user_id,))
            logs = cursor.fetchall()
            
            for log_id, context_json, greeks_json in logs:
                context = json.loads(context_json) if context_json else {}
                snapshot = context.get('poly_event_snapshot')
                
                # 模擬盈虧計算 (實務上應從行情服務獲取該期間損益)
                theoretical_loss_avoided = 750.0
                cost = 150.0
                
                score = AttributionEngine.calculate_protection_score(theoretical_loss_avoided, cost, snapshot)
                
                cursor.execute("""
                    UPDATE vtr_hedge_logs 
                    SET theoretical_pnl_delta = ?, cost_of_hedge = ?, loss_avoided = ?, protection_score = ?, status = 'CLOSED'
                    WHERE id = ?
                """, (theoretical_loss_avoided - cost, cost, theoretical_loss_avoided, score, log_id))
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to finalize VTR attribution: {e}")

    @staticmethod
    def generate_evolution_advice(user_id: int) -> Optional[str]:
        """基於歸因數據生成 NRO 參數微調建議。"""
        try:
            conn = sqlite3.connect(config.DB_NAME)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT protection_score FROM vtr_hedge_logs 
                WHERE user_id = ? AND status = 'CLOSED' 
                ORDER BY timestamp DESC LIMIT 10
            """, (user_id,))
            rows = cursor.fetchall()
            conn.close()
            
            if not rows or len(rows) < 3: return None
            
            avg_score = np.mean([r[0] for r in rows])
            
            if avg_score < 40:
                return "⚠️ **NRO 進化建議：** 偵測到近期虛擬對沖效率偏低。建議檢查 Beta 權重代理標的 (Proxy)，或將 VIX 戰情階梯的觸發靈敏度 **降低 10%** 以減少不必要的磨損。"
            elif avg_score > 85:
                return "🚀 **NRO 進化建議：** 目前對沖配置表現卓越！建議增加 **巨鯨意圖 (Taker Intent)** 的權重係數，以獲得更早的領先信號。"
            
            return None
        except Exception:
            return None

    @staticmethod
    async def generate_attribution_narration(user_id: int, log_id: int) -> str:
        """利用 LLM 生成人性化的對沖成功/失敗總結。"""
        try:
            conn = sqlite3.connect(config.DB_NAME)
            cursor = conn.cursor()
            cursor.execute("SELECT strategy_tag, pre_hedge_greeks, protection_score, loss_avoided FROM vtr_hedge_logs WHERE id = ?", (log_id,))
            row = cursor.fetchone()
            conn.close()
            
            if not row: return "查無歸因數據。"
            
            tag, greeks, score, loss = row
            
            prompt = f"""
            對沖事件歸因分析：
            - 策略標籤: {tag}
            - 初始 Greeks 狀態: {greeks}
            - 效能評分: {score:.1f}/100
            - 避免的損失: ${loss:,.0f}
            
            請以資深量化分析師的口吻，用繁體中文解釋該筆對沖的成敗原因。
            重點放在 Delta 是否被中和、Vega 曝險是否過大，以及 Gamma 帶來的非線性影響。
            字數 100 字以內，語氣專業精煉。
            """
            
            from services.llm_service import client, LLM_MODEL_NAME
            response = await client.chat.completions.create(
                model=LLM_MODEL_NAME,
                messages=[{"role": "system", "content": "You are a Quant Attribution Analyst."}, {"role": "user", "content": prompt}],
                max_tokens=250
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Failed to generate attribution narration: {e}")
            return "AI 歸因分析暫不可用。"

    @staticmethod
    def format_attribution_report(user_id: int) -> List[str]:
        """格式化對沖效能歸因報告 (Traditional Chinese)。"""
        try:
            conn = sqlite3.connect(config.DB_NAME)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT timestamp, strategy_tag, protection_score, loss_avoided, event_context FROM vtr_hedge_logs 
                WHERE user_id = ? ORDER BY timestamp DESC LIMIT 3
            """, (user_id,))
            rows = cursor.fetchall()
            conn.close()
            
            lines = ["🛡️ **【事件驅動對沖歸因報告】**\n"]
            if not rows:
                lines.append("📭 目前尚無足夠的歸因數據。")
                return lines
                
            for ts, tag, score, loss, context_json in rows:
                context = json.loads(context_json) if context_json else {}
                snapshot = context.get('poly_event_snapshot', [])
                
                event_note = ""
                if snapshot:
                    top_event = snapshot[0]
                    # 安全獲取 odds
                    distribution = top_event.get('odds_distribution', [])
                    if distribution:
                        best_odds = max([o.get('odds', 0) for o in distribution])
                        event_note = f" (驅動事件: `{top_event.get('question')[:15]}...` 機率: `{best_odds*100:.1f}%`)"
                
                lines.append(f"🔹 **{tag}** {event_note}")
                lines.append(f"  └ 分數: `{score:.1f}` | 避免損失: `${loss:,.0f}` | 時間: `{ts[5:16]}`")
            
            advice = AttributionEngine.generate_evolution_advice(user_id)
            if advice:
                lines.append(f"\n💡 **系統進化反饋:**\n{advice}")
                
            return lines
        except Exception as e:
            logger.error(f"Report failure: {e}")
            return [f"❌ 歸因報告生成失敗"]
