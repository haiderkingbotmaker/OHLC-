"""
Quotex Live OHLCV API
A complete FastAPI server for live and historical OHLCV data from Quotex
"""
import os
import json
import time
import asyncio
import traceback
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Configure logging for Hugging Face Spaces
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Add the current directory to path
import sys
sys.path.insert(0, ".")

# Load environment variables FIRST
load_dotenv()

# Print environment variables for debug (DO NOT commit passwords!)
print("="*50)
print("ENVIRONMENT VARIABLES:")
print(f"QUOTEX_EMAIL: {os.getenv('QUOTEX_EMAIL')}")
print(f"QUOTEX_PASSWORD: {'***' if os.getenv('QUOTEX_PASSWORD') else 'NOT SET'}")
print(f"QUOTEX_HOST: {os.getenv('QUOTEX_HOST')}")
print("="*50)

# Now import Quotex
try:
    from pyquotex.stable_api import Quotex
    print("✓ Successfully imported Quotex")
except Exception as e:
    print(f"✗ Failed to import Quotex: {e}")
    print(traceback.format_exc())
    raise

app = FastAPI(
    title="Quotex Live OHLCV API",
    description="API for live and historical OHLCV data from Quotex",
    version="1.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global state
quotex_client: Optional[Quotex] = None
is_connected = False
connection_lock = asyncio.Lock()
keepalive_task: Optional[asyncio.Task] = None
KEEPALIVE_INTERVAL = 45  # seconds — keeps WebSocket alive and detects drops early

# Pydantic models
class OHLCVCandle(BaseModel):
    time: int
    open: float
    high: float
    low: float
    close: float
    ticks: Optional[int] = 0

class HistoricalDataRequest(BaseModel):
    asset: str
    period: int = 60
    days: float = 1.0

class APIStatus(BaseModel):
    connected: bool
    email: Optional[str] = None
    timestamp: float

class AssetInfo(BaseModel):
    name: str
    code: str
    open: bool

# Helper functions
def get_client() -> Quotex:
    """Get the Quotex client instance"""
    global quotex_client
    if quotex_client is None:
        email = os.getenv("QUOTEX_EMAIL")
        password = os.getenv("QUOTEX_PASSWORD")
        host = os.getenv("QUOTEX_HOST", "qxbroker.com")
        if not email or not password:
            raise HTTPException(status_code=500, detail="QUOTEX_EMAIL and QUOTEX_PASSWORD must be set in environment variables")

        # Optional proxy, e.g. QUOTEX_PROXY=http://user:pass@host:port
        # Needed when the host IP is blocked by Cloudflare (most datacenter hosts).
        proxy = os.getenv("QUOTEX_PROXY")
        proxies = {"http": proxy, "https": proxy} if proxy else None

        print(f"Creating Quotex client with host: {host} (proxy: {'yes' if proxy else 'no'})")
        quotex_client = Quotex(
            email=email,
            password=password,
            host=host,
            lang="en",
            proxies=proxies,
        )
        quotex_client.debug_ws_enable = False
    return quotex_client

async def _connection_keepalive():
    """Background task: ping Quotex WebSocket and auto-reconnect if dropped."""
    global is_connected, quotex_client
    while True:
        await asyncio.sleep(KEEPALIVE_INTERVAL)
        try:
            if quotex_client and is_connected:
                alive = await quotex_client.check_connect()
                if not alive:
                    print("Keepalive: connection lost, will reconnect on next request")
                    is_connected = False
                    quotex_client = None
                elif quotex_client.api and quotex_client.api.websocket_client:
                    try:
                        quotex_client.api.send_websocket_request('42["tick"]', no_force_send=False)
                    except Exception:
                        is_connected = False
                        quotex_client = None
        except Exception as e:
            print(f"Keepalive error: {e}")
            is_connected = False
            quotex_client = None

async def ensure_connected():
    """Ensure we're connected to Quotex"""
    global is_connected, quotex_client, keepalive_task

    async with connection_lock:
        try:
            if quotex_client and is_connected:
                is_valid = await quotex_client.check_connect()
                if is_valid:
                    return quotex_client
                print("Existing connection no longer valid, resetting...")
                quotex_client = None
                is_connected = False

            client = get_client()
            if not is_connected:
                print("Connecting to Quotex...")
                check, msg = await client.connect()
                if not check:
                    if "429" in str(msg) or "Too Many Requests" in str(msg):
                        raise Exception(
                            "Cloudflare rate limit (429): Quotex blocked too many connection "
                            "attempts from this server. Wait a few minutes and retry, or use "
                            f"/connect to force reconnect. Detail: {msg}"
                        )
                    raise Exception(f"Failed to connect: {msg}")
                is_connected = True
                print("✓ Connected successfully!")

            final_check = await client.check_connect()
            if not final_check:
                print("Final connection check failed, resetting...")
                quotex_client = None
                is_connected = False
                raise Exception("Connection check failed after connect attempt")

            if keepalive_task is None or keepalive_task.done():
                keepalive_task = asyncio.create_task(_connection_keepalive())

            return client
        except HTTPException:
            raise
        except Exception as e:
            print(f"✗ Connection failed: {e}")
            print(traceback.format_exc())
            quotex_client = None
            is_connected = False
            raise HTTPException(status_code=500, detail=str(e) or repr(e))

# API Routes
@app.on_event("startup")
async def startup_event():
    """
    Start serving immediately and warm up the Quotex connection in the background.

    Connecting can take a long time (Cloudflare retries), so we must NOT block
    startup — otherwise the Space/page appears to never load.
    """
    print("🚀 Starting up API...")

    async def _warmup():
        try:
            await ensure_connected()
            print("✓ Background connection established")
        except Exception as e:
            print(f"Background connection failed (will retry on first request): {e}")

    asyncio.create_task(_warmup())

@app.get("/", response_model=APIStatus)
async def root():
    """Get API status"""
    return APIStatus(
        connected=is_connected,
        email=os.getenv("QUOTEX_EMAIL"),
        timestamp=time.time()
    )

@app.post("/connect")
async def connect():
    """Force (re-)connect to Quotex"""
    global is_connected, quotex_client
    is_connected = False
    quotex_client = None
    try:
        await ensure_connected()
        return {"status": "connected", "success": True}
    except Exception as e:
        print(f"Connect error: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/assets", response_model=List[Dict])
async def get_assets():
    """Get list of available assets"""
    client = await ensure_connected()
    instruments = await client.get_instruments()
    assets = []
    for inst in instruments:
        if inst[0]:
            assets.append({
                "code": inst[1],
                "name": inst[2].replace("\n", ""),
                "open": inst[14],
                "payout_1m": inst[-9],
                "payout_5m": inst[-8]
            })
    return assets

@app.post("/historical", response_model=List[OHLCVCandle])
async def get_historical_data(request: HistoricalDataRequest):
    """
    Get historical OHLCV data
    - asset: Asset code (e.g., "USDPKR_otc", "EURUSD_otc")
    - period: Timeframe in seconds (60=1m, 300=5m, etc.)
    - days: Number of days of history to fetch
    """
    client = await ensure_connected()
    
    # Get asset (handle OTC automatically)
    asset_name, asset_data = await client.get_available_asset(request.asset, force_open=True)
    
    amount_of_seconds = int(request.days * 86400)
    candles = await client.get_candles_deep(
        asset_name, 
        amount_of_seconds, 
        request.period
    )
    
    # Clean and validate candles
    cleaned_candles = []
    for c in candles:
        if c.get("open") is not None and c.get("close") is not None:
            cleaned_candles.append({
                "time": c["time"],
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "ticks": c.get("ticks", 0)
            })
    
    return cleaned_candles

@app.get("/realtime/{asset}")
async def get_realtime_data(asset: str, period: int = 60):
    """
    Get realtime OHLCV data (last candle)
    - asset: Asset code
    - period: Timeframe in seconds
    """
    client = await ensure_connected()
    
    asset_name, _ = await client.get_available_asset(asset, force_open=True)
    
    # Start streaming
    client.start_candles_stream(asset_name, period)
    
    # Get recent candles
    candles = await client.get_candles(asset_name, time.time(), 3600, period)
    
    if candles:
        last_candle = candles[-1]
        return {
            "asset": asset_name,
            "period": period,
            "candle": {
                "time": last_candle["time"],
                "open": float(last_candle["open"]),
                "high": float(last_candle["high"]),
                "low": float(last_candle["low"]),
                "close": float(last_candle["close"]),
                "ticks": last_candle.get("ticks", 0)
            },
            "datetime": datetime.fromtimestamp(last_candle["time"]).isoformat()
        }
    else:
        raise HTTPException(status_code=404, detail="No data available")

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "connected": is_connected,
        "timestamp": time.time()
    }

@app.get("/quotex_candles")
async def get_quotex_candles(
    asset: str = "USDBDT_otc",
    timeframe: str = "M1",
    count: int = 100
):
    """
    Simple endpoint like your example!
    Timeframes available: 5s, 10s, 15s, 30s, M1, M2, M3, M5, M10, M15, M30, H1, H2, H4, D1
    Example: /quotex_candles?asset=USDBDT_otc&timeframe=M1&count=100000
    """
    try:
        client = await ensure_connected()
        
        # Map timeframe to seconds
        timeframe_map = {
            "5s": 5,
            "10s": 10,
            "15s": 15,
            "30s": 30,
            "M1": 60,
            "M2": 120,
            "M3": 180,
            "M5": 300,
            "M10": 600,
            "M15": 900,
            "M30": 1800,
            "H1": 3600,
            "H2": 7200,
            "H4": 14400,
            "D1": 86400
        }
        if timeframe not in timeframe_map:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid timeframe '{timeframe}'. Available: {', '.join(timeframe_map)}"
            )
        period = timeframe_map[timeframe]
        
        # Calculate days needed - add 30% buffer for weekends/holidays
        candles_needed = max(count, 100)
        days_needed = (candles_needed * period) / 86400 * 1.3 + 0.5  # 30% buffer + 0.5 extra
        
        # Get asset
        asset_name, _ = await client.get_available_asset(asset, force_open=True)
        print(f"Using asset: {asset_name}, timeframe: {timeframe} ({period}s)")
        
        # Get candles with progress callback
        print(f"Fetching {count} candles (requesting {days_needed:.1f} days)...")
        
        def progress_callback(fetched_secs, total_secs, candle_count):
            pct = min((fetched_secs / total_secs) * 100, 100) if total_secs > 0 else 0
            print(f"Progress: {pct:.1f}% - {candle_count} candles so far", end="\r")
        
        all_candles = await client.get_candles_deep(
            asset_name,
            int(days_needed * 86400),
            period,
            progress_callback=progress_callback
        )
        print(f"\nGot {len(all_candles)} candles total")
        
        # Take last N candles, oldest -> newest, as standard numeric OHLC
        formatted = []
        candles_to_use = all_candles[-count:] if len(all_candles) >= count else all_candles

        for candle in candles_to_use:
            open_price = candle.get("open")
            close_price = candle.get("close")

            if open_price is None or close_price is None:
                continue

            formatted.append({
                "time": int(candle["time"]),
                "open": float(open_price),
                "high": float(candle.get("high", open_price)),
                "low": float(candle.get("low", open_price)),
                "close": float(close_price),
                "volume": int(candle.get("ticks", 0)),
            })

        if not formatted:
            raise HTTPException(
                status_code=503,
                detail=f"No candle data returned for {asset_name} ({timeframe}). "
                       "The broker connection may be blocked or rate-limited from this host."
            )

        print(f"Returning {len(formatted)} candles (oldest -> newest)")
        return formatted
    except HTTPException:
        raise
    except Exception as e:
        error_msg = f"Error: {str(e) or repr(e)}"
        print(f"\nError in /quotex_candles: {error_msg}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=error_msg)

@app.get("/all_assets_with_links")
async def get_all_assets_with_links(request: Request):
    """
    Get ALL available assets with pre-built links for 100 and 500 candles!
    Includes 5s, 10s, 15s, 30s timeframes too!
    """
    try:
        client = await ensure_connected()
        instruments = await client.get_instruments()
        
        base_url = os.getenv("PUBLIC_BASE_URL") or str(request.base_url).rstrip("/")
        all_assets = []
        
        for inst in instruments:
            if inst[0]:
                asset_code = inst[1]
                asset_name = inst[2].replace("\n", "").strip()
                is_open = inst[14]
                
                all_assets.append({
                    "asset_code": asset_code,
                    "asset_name": asset_name,
                    "is_market_open": is_open,
                    "links": {
                        # 5-second candles
                        "100_candles_5s": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=5s&count=100",
                        "500_candles_5s": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=5s&count=500",
                        # 10-second candles
                        "100_candles_10s": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=10s&count=100",
                        "500_candles_10s": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=10s&count=500",
                        # 15-second candles
                        "100_candles_15s": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=15s&count=100",
                        "500_candles_15s": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=15s&count=500",
                        # 30-second candles
                        "100_candles_30s": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=30s&count=100",
                        "500_candles_30s": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=30s&count=500",
                        # Minute/hour candles
                        "100_candles_M1": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=M1&count=100",
                        "500_candles_M1": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=M1&count=500",
                        "100_candles_M2": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=M2&count=100",
                        "500_candles_M2": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=M2&count=500",
                        "100_candles_M3": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=M3&count=100",
                        "500_candles_M3": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=M3&count=500",
                        "100_candles_M10": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=M10&count=100",
                        "500_candles_M10": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=M10&count=500",
                        "100_candles_M5": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=M5&count=100",
                        "500_candles_M5": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=M5&count=500",
                        "100_candles_M15": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=M15&count=100",
                        "500_candles_M15": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=M15&count=500",
                        "100_candles_H1": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=H1&count=100",
                        "500_candles_H1": f"{base_url}/quotex_candles?asset={asset_code}&timeframe=H1&count=500"
                    }
                })
        
        return all_assets
    except Exception as e:
        print(f"Error in /all_assets_with_links: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    # For Hugging Face Spaces, use port 7860 with long timeouts for large candles!
    port = int(os.getenv("PORT", 7860))
    print(f"Starting server on port {port}...")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        timeout_keep_alive=300,  # Keep connections alive longer
        timeout_graceful_shutdown=300,
        limit_concurrency=10
    )
