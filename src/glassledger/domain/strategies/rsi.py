from decimal import Decimal
from typing import Any, Tuple
from glassledger.domain.models import Signal, Tick
from glassledger.domain.strategies.base import BaseStrategy


class RSIStrategy(BaseStrategy):
    """
    Wilder's Relative Strength Index (RSI) pure implementation.
    Parameters:
      - period: int (default: 14)
      - oversold: Decimal (default: 30.0000)
      - overbought: Decimal (default: 70.0000)
    """

    @property
    def strategy_type(self) -> str:
        return "RSI_THRESHOLD"

    def evaluate(
        self,
        state: dict[str, Any],
        event: Tick,
        parameters: dict[str, Any],
    ) -> Tuple[dict[str, Any], Signal, dict[str, Any]]:
        period = int(parameters.get("period", 14))
        oversold = Decimal(str(parameters.get("oversold", "30.0000")))
        overbought = Decimal(str(parameters.get("overbought", "70.0000")))

        # State unpacking
        prev_price_str = state.get("last_price")
        prev_avg_gain_str = state.get("avg_gain")
        prev_avg_loss_str = state.get("avg_loss")
        count = int(state.get("count", 0))
        gains_buffer = [Decimal(g) for g in state.get("gains_buffer", [])]
        losses_buffer = [Decimal(l) for l in state.get("losses_buffer", [])]
        in_oversold = bool(state.get("in_oversold", False))
        in_overbought = bool(state.get("in_overbought", False))

        current_price = event.price

        if prev_price_str is None:
            # First tick: initialize state
            new_state = {
                "last_price": str(current_price),
                "avg_gain": "0.0000",
                "avg_loss": "0.0000",
                "count": 1,
                "gains_buffer": [],
                "losses_buffer": [],
                "in_oversold": False,
                "in_overbought": False,
            }
            snapshot = {
                "rsi": "50.0000",
                "avg_gain": "0.0000",
                "avg_loss": "0.0000",
                "status": "INITIALIZING",
            }
            return new_state, Signal.HOLD, snapshot

        prev_price = Decimal(prev_price_str)
        delta = current_price - prev_price
        current_gain = max(Decimal("0.0000"), delta)
        current_loss = max(Decimal("0.0000"), -delta)

        count += 1
        period_dec = Decimal(str(period))

        if count <= period:
            # Warm-up phase: accumulate buffer
            gains_buffer.append(current_gain)
            losses_buffer.append(current_loss)

            if count == period:
                # First average is standard SMA
                avg_gain = sum(gains_buffer) / period_dec
                avg_loss = sum(losses_buffer) / period_dec
            else:
                avg_gain = Decimal("0.0000")
                avg_loss = Decimal("0.0000")

            rsi = Decimal("50.0000")
            signal = Signal.HOLD
        else:
            # Wilder's smoothing
            prev_avg_gain = Decimal(prev_avg_gain_str or "0.0000")
            prev_avg_loss = Decimal(prev_avg_loss_str or "0.0000")

            avg_gain = ((prev_avg_gain * (period_dec - Decimal("1"))) + current_gain) / period_dec
            avg_loss = ((prev_avg_loss * (period_dec - Decimal("1"))) + current_loss) / period_dec

            if avg_loss == Decimal("0.0000"):
                rsi = Decimal("100.0000")
            else:
                rs = avg_gain / avg_loss
                rsi = Decimal("100.0000") - (Decimal("100.0000") / (Decimal("1.0000") + rs))

            # Signal determination on state transition
            signal = Signal.HOLD
            if rsi < oversold:
                if not in_oversold:
                    signal = Signal.BUY
                in_oversold = True
                in_overbought = False
            elif rsi > overbought:
                if not in_overbought:
                    signal = Signal.SELL
                in_overbought = True
                in_oversold = False
            else:
                in_oversold = False
                in_overbought = False

        new_state = {
            "last_price": str(current_price),
            "avg_gain": f"{avg_gain:.6f}",
            "avg_loss": f"{avg_loss:.6f}",
            "count": count,
            "gains_buffer": [str(g) for g in gains_buffer[-period:]],
            "losses_buffer": [str(l) for l in losses_buffer[-period:]],
            "in_oversold": in_oversold,
            "in_overbought": in_overbought,
        }

        snapshot = {
            "rsi": f"{rsi:.4f}",
            "avg_gain": f"{avg_gain:.4f}",
            "avg_loss": f"{avg_loss:.4f}",
            "delta": f"{delta:.4f}",
            "count": count,
        }

        return new_state, signal, snapshot
