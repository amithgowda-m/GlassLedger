import csv
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator
from glassledger.domain.models import Tick


class Sequencer:
    """
    Parses CSV market ticks and assigns strictly monotonic event sequence numbers.
    """

    @staticmethod
    def load_csv(csv_path: Path | str) -> list[Tick]:
        ticks: list[Tick] = []
        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError(f"Dataset CSV not found at: {path}")

        with open(path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts_str = row["timestamp"]
                # Parse ISO timestamp
                if "Z" in ts_str:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                else:
                    ts = datetime.fromisoformat(ts_str)
                if not ts.tzinfo:
                    ts = ts.replace(tzinfo=timezone.utc)

                tick = Tick(
                    event_id=row["event_id"],
                    symbol=row["symbol"],
                    price=Decimal(row["price"]),
                    volume=int(row["volume"]),
                    timestamp=ts,
                )
                ticks.append(tick)
        return ticks

    @staticmethod
    def stream_ticks(ticks: list[Tick], drop_seq: int | None = None) -> Iterator[tuple[int, Tick]]:
        """
        Yields (evaluation_seq, Tick) monotonically starting at 1.
        If drop_seq is provided, omits that exact sequence number.
        """
        seq = 1
        for tick in ticks:
            if drop_seq is not None and seq == drop_seq:
                seq += 1
                continue
            yield seq, tick
            seq += 1
