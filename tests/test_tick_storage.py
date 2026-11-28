"""Saving ticks is idempotent per (dataset_name, event_id): re-importing a dataset never duplicates it."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import uuid

from glassledger.domain.models import Tick
from glassledger.storage.mongo import MongoStorage


def _ticks(n: int, prefix: str) -> list[Tick]:
    t0 = datetime(2026, 1, 1, 9, 30, tzinfo=timezone.utc)
    return [
        Tick(event_id=f"{prefix}_{i:04d}", symbol="INFY", price=Decimal("1500.0000") + i,
             volume=100, timestamp=t0 + timedelta(seconds=i))
        for i in range(n)
    ]


def test_reimporting_the_same_ticks_inserts_nothing():
    mongo = MongoStorage()
    name = f"dedup_{uuid.uuid4().hex[:8]}"
    ticks = _ticks(5, name)
    assert mongo.save_ticks(ticks, dataset_name=name) == 5
    assert mongo.save_ticks(ticks, dataset_name=name) == 0
    assert len(mongo.get_ticks(name)) == 5


def test_partial_overlap_inserts_only_new_ticks_and_batch_duplicates_once():
    mongo = MongoStorage()
    name = f"dedup_{uuid.uuid4().hex[:8]}"
    first = _ticks(3, name)
    mongo.save_ticks(first, dataset_name=name)
    more = _ticks(6, name)                     # ids 0..5, of which 0..2 already stored
    assert mongo.save_ticks(more + more[4:], dataset_name=name) == 3   # 3,4,5 once each
    assert [t.event_id for t in mongo.get_ticks(name)] == [t.event_id for t in more]


def test_same_event_id_in_another_dataset_is_independent():
    mongo = MongoStorage()
    ticks = _ticks(2, "shared")
    a, b = f"ds_a_{uuid.uuid4().hex[:6]}", f"ds_b_{uuid.uuid4().hex[:6]}"
    assert mongo.save_ticks(ticks, dataset_name=a) == 2
    assert mongo.save_ticks(ticks, dataset_name=b) == 2
