from decimal import Decimal
from typing import Any, Tuple
from glassledger.domain.models import Signal, Tick
from glassledger.domain.strategies.base import BaseStrategy


class MACrossoverStrategy(BaseStrategy):
    """
    Dual Moving Average Crossover pure implementation.
    Parameters:
      - fast_period: int (default: 10)
      - slow_period: int (default: 30)
    """

    @property
    def strategy_type(self) -> str:
        return "MA_CROSSOVER"

    def evaluate(
        self,
        state: dict[str, Any],
        event: Tick,
        parameters: dict[str, Any],
    ) -> Tuple[dict[str, Any], Signal, dict[str, Any]]:
        fast_period = int(parameters.get("fast_period", 10))
        slow_period = int(parameters.get("slow_period", 30))

        if fast_period >= slow_period:
            raise ValueError(f"fast_period ({fast_period}) must be strictly less than slow_period ({slow_period})")

        history = [Decimal(p) for p in state.get("price_history", [])]
        prev_fast_sma_str = state.get("prev_fast_sma")
        prev_slow_sma_str = state.get("prev_slow_sma")

        current_price = event.price
        history.append(current_price)

        # Keep only what is needed for slow_period
        if len(history) > slow_period:
            history = history[-slow_period:]

        fast_sma: Decimal | None = None
        slow_sma: Decimal | None = None
        signal = Signal.HOLD

        if len(history) >= fast_period:
            fast_window = history[-fast_period:]
            fast_sma = sum(fast_window) / Decimal(str(fast_period))

        if len(history) >= slow_period:
            slow_window = history[-slow_period:]
            slow_sma = sum(slow_window) / Decimal(str(slow_period))

        if fast_sma is not None and slow_sma is not None:
            if prev_fast_sma_str is not None and prev_slow_sma_str is not None:
                prev_fast = Decimal(prev_fast_sma_str)
                prev_slow = Decimal(prev_slow_sma_str)

                # Golden Cross: fast crosses above slow
                if prev_fast <= prev_slow and fast_sma > slow_sma:
                    signal = Signal.BUY
                # Death Cross: fast crosses below slow
                elif prev_fast >= prev_slow and fast_sma < slow_sma:
                    signal = Signal.SELL

        new_state = {
            "price_history": [str(p) for p in history],
            "prev_fast_sma": str(fast_sma) if fast_sma is not None else None,
            "prev_slow_sma": str(slow_sma) if slow_sma is not None else None,
        }

        snapshot = {
            "current_price": str(current_price),
            "fast_sma": str(fast_sma) if fast_sma is not None else None,
            "slow_sma": str(slow_sma) if slow_sma is not None else None,
            "history_length": len(history),
        }

        return new_state, signal, snapshot
