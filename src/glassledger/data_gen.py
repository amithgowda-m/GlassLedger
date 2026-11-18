import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


def generate_synthetic_dataset(output_path: Path, count: int = 2000, symbol: str = "AAPL") -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Base timestamp
    start_time = datetime(2026, 1, 1, 9, 30, 0, tzinfo=timezone.utc)
    base_price = Decimal("150.0000")
    
    with open(output_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["event_id", "symbol", "price", "volume", "timestamp"])
        
        current_price = base_price
        for i in range(1, count + 1):
            ts = start_time + timedelta(seconds=i)
            event_id = f"evt_{i:06d}"
            
            # Construct cyclical trends so RSI hits < 30 and > 70, and SMAs cross
            # Cycle every 200 ticks:
            # 0..60: downward pressure (triggers oversold RSI < 30 -> BUY)
            # 61..120: strong upward momentum (triggers golden cross & RSI > 70 -> SELL)
            # 121..200: gentle consolidation
            cycle_pos = i % 200
            if cycle_pos < 50:
                delta = Decimal("-0.3500")
            elif cycle_pos < 110:
                delta = Decimal("0.4000")
            elif cycle_pos < 160:
                delta = Decimal("-0.1500")
            else:
                delta = Decimal("0.1000")
                
            current_price = max(Decimal("10.0000"), current_price + delta)
            volume = 100 + (i % 50) * 10
            
            writer.writerow([
                event_id,
                symbol,
                f"{current_price:.4f}",
                volume,
                ts.isoformat()
            ])


if __name__ == "__main__":
    generate_synthetic_dataset(Path("data/synthetic_ticks.csv"), count=2000)
    print("Generated 2000 synthetic ticks in data/synthetic_ticks.csv")
