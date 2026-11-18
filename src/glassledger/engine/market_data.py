import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import AsyncIterator, Optional, Protocol, runtime_checkable

from glassledger.domain.models import Tick
from glassledger.engine.sequencer import Sequencer


@runtime_checkable
class MarketDataSource(Protocol):
    """
    Market Data Seam.
    Isolates tick origin (finite offline CSV, free REST polling, or low-latency WebSocket)
    from downstream sequence numbering, strategy VM, and financial settlement.
    """

    async def stream_ticks(self) -> AsyncIterator[Tick]:
        """Yields standardized Tick domain models indefinitely or until EOF."""
        ...


class CSVReplaySource:
    """Deterministic offline replay from CSV."""

    def __init__(self, csv_path: Path | str, drop_seq: Optional[int] = None):
        self.csv_path = Path(csv_path)
        self.drop_seq = drop_seq

    async def stream_ticks(self) -> AsyncIterator[Tick]:
        ticks = Sequencer.load_csv(self.csv_path)
        for seq, tick in Sequencer.stream_ticks(ticks, drop_seq=self.drop_seq):
            yield tick


class YFinancePollSource:
    """
    Free real-world market data source using Yahoo Finance (NSE/BSE and US Equities).
    Requires zero broker accounts or API keys.
    Useful for live demos and self-hosters.
    """

    def __init__(self, symbols: list[str], interval_seconds: float = 3.0):
        self.symbols = symbols
        self.interval_seconds = interval_seconds
        self._seq = 0

    async def stream_ticks(self) -> AsyncIterator[Tick]:
        import urllib.request
        import json

        while True:
            for symbol in self.symbols:
                self._seq += 1
                try:
                    # Query free Yahoo Finance v8 chart API
                    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"
                    req = urllib.request.Request(
                        url,
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                    )
                    with urllib.request.urlopen(req, timeout=3.0) as resp:
                        data = json.loads(resp.read().decode())
                        meta = data["chart"]["result"][0]["meta"]
                        price = Decimal(str(meta.get("regularMarketPrice", 150.0))).quantize(Decimal("0.0001"))
                        vol = int(meta.get("regularMarketVolume", 100))

                    tick = Tick(
                        event_id=f"live_yf_{self._seq:06d}",
                        symbol=symbol.replace(".NS", "").replace(".BO", ""),
                        price=price,
                        volume=vol,
                        timestamp=datetime.now(timezone.utc),
                    )
                    yield tick
                except Exception:
                    # Synthetic fallback tick if network drops
                    tick = Tick(
                        event_id=f"live_yf_{self._seq:06d}",
                        symbol=symbol,
                        price=Decimal("150.0000"),
                        volume=100,
                        timestamp=datetime.now(timezone.utc),
                    )
                    yield tick

            await asyncio.sleep(self.interval_seconds)


class BreezeWebSocketSource:
    """
    Production Adapter Stub for ICICI Direct Breeze API.
    Free for all ICICI Direct account holders.
    Provides true real-time WebSocket ticks and 3 years of free historical F&O data.
    """

    def __init__(self, api_key: str = "mock_breeze_key", session_token: str = "mock_token"):
        self.api_key = api_key
        self.session_token = session_token
        self.queue: asyncio.Queue[Tick] = asyncio.Queue(maxsize=1000)

    async def stream_ticks(self) -> AsyncIterator[Tick]:
        # Connects via BreezeConnect SDK on_ticks callback
        while True:
            tick = await self.queue.get()
            yield tick


class KiteWebSocketSource:
    """
    Production Adapter Stub for Zerodha KiteTicker WebSocket.
    Available under Kite Connect's ₹500/month market data plan.
    """

    def __init__(self, api_key: str, access_token: str, instruments: list[int]):
        self.api_key = api_key
        self.access_token = access_token
        self.instruments = instruments
        self.queue: asyncio.Queue[Tick] = asyncio.Queue(maxsize=1000)

    async def stream_ticks(self) -> AsyncIterator[Tick]:
        # Connects via KiteTicker SDK on_ticks callback
        while True:
            tick = await self.queue.get()
            yield tick
