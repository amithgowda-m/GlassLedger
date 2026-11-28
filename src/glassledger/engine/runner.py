from decimal import Decimal
from pathlib import Path
from typing import Any, Optional
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from glassledger.config import settings
from glassledger.domain.models import (
    DecisionSnapshot,
    OrderIntent,
    OrderSide,
    OrderStatus,
    Signal,
    Tick,
)
from glassledger.domain.strategies import get_strategy
from glassledger.engine.broker import BrokerAdapter, get_broker
from glassledger.engine.sequencer import Sequencer
from glassledger.storage.mongo import MongoStorage
from glassledger.storage.postgres import PostgresStorage


class Runner:
    def __init__(
        self,
        pg_storage: Optional[PostgresStorage] = None,
        mongo_storage: Optional[MongoStorage] = None,
        broker: Optional[BrokerAdapter] = None,
    ):
        self.pg = pg_storage or PostgresStorage()
        self.mongo = mongo_storage or MongoStorage()
        self.broker = broker or get_broker(settings.default_broker)

    def run_strategy(
        self,
        dataset_name: str,
        strategy_id: str,
        run_id: str,
        drop_seq: Optional[int] = None,
        fail_before_settlement_seq: Optional[int] = None,
        show_progress: bool = True,
    ) -> dict[str, Any]:
        """
        Executes a deterministic trading run over the dataset.
        Enforces FR-12 Write Ordering: Mongo decision envelope logged FIRST,
        followed by PostgreSQL atomic order reservation and settlement.
        """
        # Fetch strategy configuration from Mongo or register default
        strat_doc = self.mongo.get_strategy(strategy_id)
        if not strat_doc:
            strategy_type = "RSI_THRESHOLD" if "rsi" in strategy_id.lower() else "MA_CROSSOVER"
            parameters = {"period": 14, "oversold": "30.0000", "overbought": "70.0000"} if strategy_type == "RSI_THRESHOLD" else {"fast_period": 10, "slow_period": 30}
        else:
            strategy_type = strat_doc["strategy_type"]
            parameters = strat_doc.get("parameters", {})

        strategy = get_strategy(strategy_type)
        ticks = self.mongo.get_ticks(dataset_name)
        if not ticks:
            # Auto-bootstrap baseline_ticks if synthetic_ticks.csv exists
            synth_path = Path("data/synthetic_ticks.csv")
            if dataset_name in ("baseline_ticks", "default") and synth_path.exists():
                loaded_ticks = Sequencer.load_csv(str(synth_path))
                self.mongo.save_ticks(loaded_ticks, dataset_name=dataset_name)
                ticks = self.mongo.get_ticks(dataset_name)

        if not ticks:
            raise ValueError(
                f"No ticks found in MongoDB for dataset: '{dataset_name}'. "
                f"Seed default ticks with `glassledger db migrate` or import via "
                f"`glassledger dataset import data/synthetic_ticks.csv --name {dataset_name}`."
            )

        state: dict[str, Any] = {}
        ticks_evaluated = 0
        decisions_logged = 0
        orders_placed = 0
        fills_settled = 0
        orders_rejected = 0

        # Run stream with optional dropped tick injection
        tick_stream = Sequencer.stream_ticks(ticks, drop_seq=drop_seq)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            disable=not show_progress,
        ) as progress:
            task = progress.add_task(f"[cyan]Executing {strategy_id} on {dataset_name}...", total=len(ticks))

            for seq, tick in tick_stream:
                # 1. Pure VM evaluation
                new_state, signal, snapshot = strategy.evaluate(state, tick, parameters)
                state = new_state
                ticks_evaluated += 1

                decision_id = f"dec_{run_id}_{seq:06d}"
                order_id: Optional[str] = None
                side: Optional[OrderSide] = None
                order: Optional[OrderIntent] = None

                if signal in (Signal.BUY, Signal.SELL):
                    order_id = f"ord_{run_id}_{seq:06d}"
                    side = OrderSide.BUY if signal == Signal.BUY else OrderSide.SELL
                    qty = 10  # Standard lot size

                    if side == OrderSide.BUY:
                        # Estimate reservation with fee and slippage cushion
                        estimated_price = tick.price + settings.default_slippage
                        cost = Decimal(qty) * estimated_price
                        reserved_amount = (cost * (Decimal("1.0000") + settings.default_fee_rate)).quantize(Decimal("0.0001"))
                    else:
                        reserved_amount = Decimal("0.0000")

                    order = OrderIntent(
                        order_id=order_id,
                        investor_id=1,
                        symbol=tick.symbol,
                        side=side,
                        quantity=qty,
                        price=tick.price,
                        reserved_amount=reserved_amount,
                        status=OrderStatus.PENDING,
                        decision_ref=decision_id,
                    )

                # 2. FR-12 REQUIRED WRITE ORDERING: Write decision to Mongo FIRST
                decision = DecisionSnapshot(
                    decision_id=decision_id,
                    strategy_id=f"{strategy_id}@{run_id}",
                    evaluation_seq=seq,
                    trigger_tick={
                        "event_id": tick.event_id,
                        "symbol": tick.symbol,
                        "price": str(tick.price),
                        "volume": tick.volume,
                        "timestamp": tick.timestamp.isoformat(),
                    },
                    indicator_state=snapshot,
                    signal=signal,
                    order_id=order_id,
                )
                self.mongo.log_decision(decision)
                decisions_logged += 1

                # FR-12 Failure injection hook between writes
                if fail_before_settlement_seq is not None and seq == fail_before_settlement_seq:
                    raise RuntimeError(
                        f"INJECTED_FAILURE_AFTER_MONGO_BEFORE_POSTGRES: Simulated crash at seq {seq}"
                    )

                # 3. SECOND: intake -> broker -> settlement in one PostgreSQL transaction.
                #    Insufficient funds/holdings or a broker refusal becomes a REJECTED order
                #    (reservations released), so the run continues and provenance is kept.
                if order is not None:
                    result = self.pg.execute_order(order, self.broker)
                    if not result.get("replayed"):
                        orders_placed += 1
                        if result["status"] == "FILLED":
                            fills_settled += 1
                        elif result["status"] == "REJECTED":
                            orders_rejected += 1

                progress.update(task, advance=1)

        investor = self.pg.get_investor_account(1)
        holdings = self.pg.get_holdings(1)
        journal = self.pg.get_journal_summary()

        return {
            "run_id": run_id,
            "dataset_name": dataset_name,
            "strategy_id": strategy_id,
            "ticks_evaluated": ticks_evaluated,
            "decisions_logged": decisions_logged,
            "orders_placed": orders_placed,
            "fills_settled": fills_settled,
            "orders_rejected": orders_rejected,
            "wallet_balance": str(investor.get("wallet_balance", "0.0000")),
            "reserved_cash": str(investor.get("reserved_cash", "0.0000")),
            "total_debit": str(journal.get("total_debit", "0.0000")),
            "total_credit": str(journal.get("total_credit", "0.0000")),
            "holdings": holdings,
        }
