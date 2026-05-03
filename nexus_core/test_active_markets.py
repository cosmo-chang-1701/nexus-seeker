import asyncio
import httpx
import json
import datetime

POLY_API_BASE = "https://clob.polymarket.com"

async def test_active_logic():
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        # 不帶過濾器抓取 1000 個市場
        url = f"{POLY_API_BASE}/markets?limit=1000"
        active_found = []
        
        print(f"Fetching 1000 markets for discovery...")
        resp = await client.get(url)
        if resp.status_code == 200:
            data = resp.json()
            markets = data.get("data", [])
            
            for m in markets:
                ed = m.get("end_date_iso")
                if ed and ed > now_iso:
                    active_found.append({
                        "q": m.get("question")[:50],
                        "ed": ed,
                        "a": m.get("active"),
                        "c": m.get("closed"),
                        "ar": m.get("archived")
                    })
                
        print(f"\n--- Discovered {len(active_found)} Future Markets ---")
        for i, m in enumerate(active_found[:20]):
            print(f"{i+1}. {m['q']} | Active: {m['a']} | Closed: {m['c']} | Archived: {m['ar']} | Ends: {m['ed']}")

if __name__ == "__main__":
    asyncio.run(test_active_logic())
