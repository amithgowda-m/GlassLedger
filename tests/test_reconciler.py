from datetime import datetime, timezone
from decimal import Decimal
import pytest

from glassledger.diagnostics.reconciler import CrossStoreReconciler
from glassledger.domain.models import (
    DecisionSnapshot,
    OrderIntent,
    OrderSide,
    OrderStatus,
    Signal,
    Tick,
)
from glassledger.engine.broker import PaperBroker
from glassledger.engine.runner import Runner
from glassledger.storage.mongo import MongoStorage
from glassledger.storage.postgres import PostgresStorage


def test_reconciler_on_consistent_run():
    """
    FR-12 Verification:
    Proves that a normal simulation run adheres to write ordering and
    reconciler reports 100% cross-store consistency with zero orphans.
    """
    pg = PostgresStorage()
    mongo = MongoStorage()

    # Seed test tick in mongo
    test_run_id = "test_recon_clean_01"
    tick = Tick(
        event_id="evt_recon_01",
        symbol="RELIANCE",
        price=Decimal("2800.0000"),
        volume=1000,
        timestamp=datetime.now(timezone.utc),
    )
    mongo.save_ticks([tick], dataset_name="recon_clean_dataset")

    runner = Runner(pg_storage=pg, mongo_storage=mongo, broker=PaperBroker())
    summary = runner.run_strategy(
        dataset_name="recon_clean_dataset",
        strategy_id="rsi_v1",
        run_id=test_run_id,
        show_progress=False,
    )

    reconciler = CrossStoreReconciler(pg=pg, mongo=mongo)
    report = reconciler.reconcile(run_id=test_run_id)

    assert report["is_consistent"] is True
    assert report["orphan_decisions_count"] == 0
    assert report["orphan_orders_count"] == 0
    assert report["orphan_fills_count"] == 0


def test_reconciler_detects_injected_failure_between_writes():
    """
    FR-12 Core Acceptance Criterion:
    Simulates a failure injected between the MongoDB decision write and PostgreSQL settlement.
    Proves that:
      1. Decision is logged to Mongo first.
      2. Postgres settlement is aborted.
      3. The failure leaves only a harmless orphan decision.
      4. Zero orphan fills exist in Postgres (provenance chain unbroken).
      5. The Reconciler reports the orphan decision.
    """
    pg = PostgresStorage()
    mongo = MongoStorage()

    test_run_id = "test_recon_injected_fail_01"
    # Seed ticks that will trigger an actionable BUY signal
    # Generate 20 decreasing ticks to force RSI oversold (< 30)
    ticks = []
    base_price = Decimal("3000.0000")
    for i in range(1, 25):
        base_price -= Decimal("25.0000")
        ticks.append(
            Tick(
                event_id=f"evt_fail_{i:04d}",
                symbol="RELIANCE",
                price=base_price,
                volume=500,
                timestamp=datetime.now(timezone.utc),
            )
        )
    mongo.save_ticks(ticks, dataset_name="recon_fail_dataset")

    runner = Runner(pg_storage=pg, mongo_storage=mongo, broker=PaperBroker())

    # Find the sequence where a signal will be generated and inject failure at that sequence
    # Evaluate sequentially to find first BUY signal seq
    from glassledger.domain.strategies import get_strategy
    strat = get_strategy("RSI_THRESHOLD")
    params = {"period": 14, "oversold": "30.0000", "overbought": "70.0000"}
    st = {}
    signal_seq = None
    for seq_idx, t in enumerate(ticks, start=1):
        st, sig, _ = strat.evaluate(st, t, params)
        if sig == Signal.BUY:
            signal_seq = seq_idx
            break

    assert signal_seq is not None, "Test ticks should trigger a BUY signal"

    # Run with injected failure at signal_seq (after Mongo write, before Postgres settlement)
    with pytest.raises(RuntimeError, match="INJECTED_FAILURE_AFTER_MONGO_BEFORE_POSTGRES"):
        runner.run_strategy(
            dataset_name="recon_fail_dataset",
            strategy_id="rsi_v1",
            run_id=test_run_id,
            fail_before_settlement_seq=signal_seq,
            show_progress=False,
        )

    # 1. Mongo MUST have the decision recorded (write ordering: Mongo first)
    expected_dec_id = f"dec_{test_run_id}_{signal_seq:06d}"
    dec_doc = mongo.get_decision(expected_dec_id)
    assert dec_doc is not None
    assert dec_doc["decision_id"] == expected_dec_id
    assert dec_doc["order_id"] == f"ord_{test_run_id}_{signal_seq:06d}"

    # 2. PostgreSQL MUST NOT have the order or fill
    with pg.get_connection_context() as conn:
        cur = conn.cursor()
        is_sqlite = isinstance(conn, type(pg.get_sqlite_conn()))
        cur.execute(
            "SELECT COUNT(*) FROM orders WHERE order_id = ?"
            if is_sqlite
            else "SELECT COUNT(*) FROM orders WHERE order_id = %s",
            (dec_doc["order_id"],),
        )
        row = cur.fetchone()
        order_count = list(row)[0] if not isinstance(row, dict) else list(row.values())[0]
        cur.close()

    assert order_count == 0, "Postgres settlement should not have executed due to injected failure"

    # 3. Reconciler MUST identify and report the orphan decision
    reconciler = CrossStoreReconciler(pg=pg, mongo=mongo)
    report = reconciler.reconcile(run_id=test_run_id)

    assert report["is_consistent"] is False
    assert report["orphan_decisions_count"] >= 1
    assert report["orphan_orders_count"] == 0
    assert report["orphan_fills_count"] == 0

    orphan_ids = [d["decision_id"] for d in report["orphan_decisions"]]
    assert expected_dec_id in orphan_ids


def test_reconciler_detects_orphan_order_in_postgres():
    """
    Proves reconciler flags critical provenance violation if an order
    exists in PostgreSQL with no corresponding decision in MongoDB.
    """
    pg = PostgresStorage()
    mongo = MongoStorage()

    import uuid
    uid = uuid.uuid4().hex[:8]
    ghost_order_id = f"ord_phantom_ghost_{uid}"
    orphan_order = OrderIntent(
        order_id=ghost_order_id,
        investor_id=1,
        symbol="TCS",
        side=OrderSide.BUY,
        quantity=5,
        price=Decimal("3500.0000"),
        reserved_amount=Decimal("18000.0000"),
        decision_ref=f"dec_non_existent_ghost_{uid}",
    )

    with pg.get_connection_context() as conn:
        pg.submit_order_intent(conn, orphan_order)

    reconciler = CrossStoreReconciler(pg=pg, mongo=mongo)
    report = reconciler.reconcile()

    assert report["is_consistent"] is False
    assert report["orphan_orders_count"] >= 1
    orphan_order_ids = [o["order_id"] for o in report["orphan_orders"]]
    assert ghost_order_id in orphan_order_ids
