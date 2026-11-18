from abc import ABC, abstractmethod
from typing import Any, Tuple
from glassledger.domain.models import Signal, Tick


class BaseStrategy(ABC):
    """
    Pure strategy virtual machine interface.
    Rules:
    - Never call system clocks (datetime.now(), time.time()).
    - Never call random number generators (random.*).
    - Never perform I/O or network requests.
    - Output must be 100% deterministic given (state, event, parameters).
    """

    @property
    @abstractmethod
    def strategy_type(self) -> str:
        """Returns the strategy type identifier."""
        pass

    @abstractmethod
    def evaluate(
        self,
        state: dict[str, Any],
        event: Tick,
        parameters: dict[str, Any],
    ) -> Tuple[dict[str, Any], Signal, dict[str, Any]]:
        """
        Pure evaluation function.
        
        Args:
            state: Previous strategy state dictionary.
            event: The incoming market Tick.
            parameters: Polymorphic configuration parameters.

        Returns:
            Tuple of:
            - new_state: Updated state dictionary for next tick.
            - signal: BUY, SELL, or HOLD.
            - indicator_snapshot: Key/value snapshot of computed indicators for the decision log.
        """
        pass
