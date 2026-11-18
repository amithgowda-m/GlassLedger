from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator

try:
    from enum import StrEnum
except ImportError:
    class StrEnum(str, Enum):
        def __str__(self) -> str:
            return str(self.value)


class Signal(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class StrategyType(StrEnum):
    MA_CROSSOVER = "MA_CROSSOVER"
    RSI_THRESHOLD = "RSI_THRESHOLD"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"


class JournalAccount(StrEnum):
    CASH = "CASH"
    SECURITIES_AT_COST = "SECURITIES_AT_COST"
    FEE_EXPENSE = "FEE_EXPENSE"



class BaseDomainModel(BaseModel):
    model_config = ConfigDict(
        use_enum_values=True,
        arbitrary_types_allowed=True,
    )


class Tick(BaseDomainModel):
    event_id: str
    symbol: str
    price: Decimal
    volume: int
    timestamp: datetime

    @field_validator("price", mode="before")
    @classmethod
    def validate_price(cls, v: Any) -> Decimal:
        if isinstance(v, float):
            raise TypeError("Float is forbidden for price; use Decimal or str")
        d = Decimal(str(v))
        if d <= Decimal("0"):
            raise ValueError("Price must be strictly positive")
        return d


class StrategyConfig(BaseDomainModel):
    strategy_id: str
    investor_id: int
    strategy_type: StrategyType
    version: str = "1.0.0"
    is_active: bool = True
    parameters: dict[str, Any] = Field(default_factory=dict)


class DecisionSnapshot(BaseDomainModel):
    decision_id: str
    strategy_id: str
    evaluation_seq: int
    trigger_tick: dict[str, Any]
    indicator_state: dict[str, Any]
    signal: Signal
    order_id: Optional[str] = None


class OrderIntent(BaseDomainModel):
    order_id: str
    investor_id: int
    symbol: str
    side: OrderSide
    quantity: int
    price: Decimal
    reserved_amount: Decimal
    status: OrderStatus = OrderStatus.PENDING
    decision_ref: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("quantity")
    @classmethod
    def validate_quantity(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("Quantity must be greater than zero")
        return v

    @field_validator("price", "reserved_amount", mode="before")
    @classmethod
    def validate_monetary(cls, v: Any) -> Decimal:
        if isinstance(v, float):
            raise TypeError("Float is forbidden; use Decimal or str")
        return Decimal(str(v))


class FillExecution(BaseDomainModel):
    fill_id: str
    order_id: str
    quantity: int
    price: Decimal
    fee: Decimal = Decimal("0.0000")
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("price", "fee", mode="before")
    @classmethod
    def validate_monetary(cls, v: Any) -> Decimal:
        if isinstance(v, float):
            raise TypeError("Float is forbidden; use Decimal or str")
        return Decimal(str(v))


class JournalPosting(BaseDomainModel):
    entry_id: str
    fill_id: str
    ledger_account: JournalAccount
    debit: Decimal = Decimal("0.0000")
    credit: Decimal = Decimal("0.0000")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("debit", "credit", mode="before")
    @classmethod
    def validate_monetary(cls, v: Any) -> Decimal:
        if isinstance(v, float):
            raise TypeError("Float is forbidden; use Decimal or str")
        return Decimal(str(v))
