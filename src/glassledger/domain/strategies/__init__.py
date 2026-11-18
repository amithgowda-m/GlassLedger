from glassledger.domain.strategies.base import BaseStrategy
from glassledger.domain.strategies.rsi import RSIStrategy
from glassledger.domain.strategies.ma_crossover import MACrossoverStrategy

STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    "RSI_THRESHOLD": RSIStrategy,
    "MA_CROSSOVER": MACrossoverStrategy,
    "rsi_v1": RSIStrategy,
    "ma_v1": MACrossoverStrategy,
}


def get_strategy(strategy_type_or_alias: str) -> BaseStrategy:
    strategy_cls = STRATEGY_REGISTRY.get(strategy_type_or_alias)
    if not strategy_cls:
        raise ValueError(
            f"Unknown strategy: '{strategy_type_or_alias}'. Available: {list(STRATEGY_REGISTRY.keys())}"
        )
    return strategy_cls()


__all__ = [
    "BaseStrategy",
    "RSIStrategy",
    "MACrossoverStrategy",
    "STRATEGY_REGISTRY",
    "get_strategy",
]
