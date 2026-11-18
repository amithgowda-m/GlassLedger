from datetime import datetime, timezone
from decimal import Decimal
from glassledger.domain.models import Tick
from glassledger.domain.strategies.rsi import RSIStrategy
from glassledger.domain.strategies.ma_crossover import MACrossoverStrategy


def generate_test_ticks(n: int = 100) -> list[Tick]:
    base_time = datetime(2026, 1, 1, 9, 30, 0, tzinfo=timezone.utc)
    base_price = Decimal("100.0000")
    ticks = []
    current_price = base_price
    for i in range(1, n + 1):
        # Deterministic price oscillation
        delta = Decimal("0.5000") if (i % 10) < 5 else Decimal("-0.4500")
        current_price += delta
        ticks.append(
            Tick(
                event_id=f"t_{i:04d}",
                symbol="AAPL",
                price=current_price,
                volume=100,
                timestamp=base_time,
            )
        )
    return ticks


def test_rsi_pure_determinism():
    """
    FR-7 Verification:
    Proves that running the Wilder's RSI strategy twice over the same ticks
    generates 100% identical states, signals, and indicator snapshots.
    """
    ticks = generate_test_ticks(100)
    strategy = RSIStrategy()
    params = {"period": 14, "oversold": "30.0000", "overbought": "70.0000"}

    # Run 1
    state_1 = {}
    history_1 = []
    for t in ticks:
        state_1, signal_1, snap_1 = strategy.evaluate(state_1, t, params)
        history_1.append((state_1, signal_1, snap_1))

    # Run 2
    state_2 = {}
    history_2 = []
    for t in ticks:
        state_2, signal_2, snap_2 = strategy.evaluate(state_2, t, params)
        history_2.append((state_2, signal_2, snap_2))

    # Bit-for-bit parity check
    assert len(history_1) == len(history_2)
    for (s1, sig1, snap1), (s2, sig2, snap2) in zip(history_1, history_2):
        assert s1 == s2
        assert sig1 == sig2
        assert snap1 == snap2


def test_ma_crossover_pure_determinism():
    """
    FR-7 Verification:
    Proves that running the Dual SMA Crossover strategy twice over the same ticks
    generates 100% identical states, signals, and indicator snapshots.
    """
    ticks = generate_test_ticks(100)
    strategy = MACrossoverStrategy()
    params = {"fast_period": 5, "slow_period": 15}

    state_1 = {}
    history_1 = []
    for t in ticks:
        state_1, signal_1, snap_1 = strategy.evaluate(state_1, t, params)
        history_1.append((state_1, signal_1, snap_1))

    state_2 = {}
    history_2 = []
    for t in ticks:
        state_2, signal_2, snap_2 = strategy.evaluate(state_2, t, params)
        history_2.append((state_2, signal_2, snap_2))

    assert len(history_1) == len(history_2)
    for step1, step2 in zip(history_1, history_2):
        assert step1 == step2
