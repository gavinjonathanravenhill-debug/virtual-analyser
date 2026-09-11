import os, json, asyncio, logging
from datetime import datetime
from typing import Dict, List, Optional
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class WhaleTracker:
    def __init__(self, db_conn, resend_client=None):
        self.db = db_conn
        self.resend = resend_client
        self.hyperliquid_base = "https://api.hyperliquid.xyz"
    
    async def fetch_whale_perps_position(self, wallet: str) -> Dict:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                payload = {"type": "userState", "user": wallet}
                resp = await client.post(f"{self.hyperliquid_base}/info", json=payload)
                resp.raise_for_status()
                data = resp.json()
                positions = data.get("assetPositions", [])
                perps = [{"token": p.get("coin"), "size": float(p.get("szi", 0)), "entry_px": float(p.get("entryPx", 0)), "mark_px": float(p.get("markPx", 0)), "liquidation_px": float(p.get("liquidationPx", 0)), "unrealized_pnl": float(p.get("unrealizedPnl", 0)), "leverage": float(p.get("leverage", 1))} for p in positions if p.get("type") == "perp"]
                return {"wallet": wallet, "timestamp": datetime.utcnow(), "positions": perps, "raw": data}
        except Exception as e:
            logger.error(f"Failed to fetch whale perps: {e}")
            return {"wallet": wallet, "timestamp": datetime.utcnow(), "positions": [], "error": str(e)}
    
    def detect_pyramid(self, wallet_addr: str, token: str, lookback_hours: int = 24) -> Optional[Dict]:
        return None
    
    def check_liquidation_risk(self, wallet_addr: str, token: str) -> Optional[Dict]:
        return None
    
    async def send_alert_email(self, whale_addr: str, alert_type: str, details: Dict) -> bool:
        return False

def init_whale_tracker_tables(conn):
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS whale_snapshots (id SERIAL PRIMARY KEY, wallet_address VARCHAR(255), snapshot_time TIMESTAMP, positions_json JSONB, created_at TIMESTAMP DEFAULT NOW());")
    cursor.execute("CREATE TABLE IF NOT EXISTS whale_positions (id SERIAL PRIMARY KEY, snapshot_id INTEGER, wallet_address VARCHAR(255), token VARCHAR(50), size NUMERIC(20, 8), entry_px NUMERIC(20, 8), mark_px NUMERIC(20, 8), liquidation_px NUMERIC(20, 8), unrealized_pnl NUMERIC(20, 8), leverage NUMERIC(10, 2), recorded_at TIMESTAMP, created_at TIMESTAMP DEFAULT NOW());")
    cursor.execute("CREATE TABLE IF NOT EXISTS whale_alerts (id SERIAL PRIMARY KEY, wallet_address VARCHAR(255), alert_type VARCHAR(50), token VARCHAR(50), alert_data JSONB, email_sent BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT NOW());")
    conn.commit()
    logger.info("Whale tracker tables initialized")
