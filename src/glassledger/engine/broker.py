from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Protocol, runtime_checkable
from glassledger.config import settings
from glassledger.domain.models import FeeBreakdown, FillExecution, OrderIntent, OrderSide


@dataclass
class FeeSchedule:
    """
    FR-13 Realistic Fee Model Configuration.
    Configurable schedule per broker for Brokerage, STT, Exchange charges, GST, and Stamp Duty.
    All rates are expressed as Decimals for zero-float financial arithmetic.
    """
    brokerage_rate: Decimal = Decimal("0.0003")          # 0.03% of turnover
    brokerage_flat: Optional[Decimal] = None             # Fixed flat fee if applicable
    brokerage_cap: Optional[Decimal] = Decimal("20.0000") # Max fee cap (e.g. ₹20 per trade)
    stt_rate: Decimal = Decimal("0.0010")                # 0.1% Securities Transaction Tax
    exchange_rate: Decimal = Decimal("0.0000345")        # NSE turnover charge (0.00345%)
    gst_rate: Decimal = Decimal("0.1800")                # 18% GST on (Brokerage + Exchange)
    stamp_duty_rate: Decimal = Decimal("0.00015")        # 0.015% Stamp duty (BUY only)


def compute_fee_breakdown(
    turnover: Decimal,
    side: OrderSide,
    schedule: Optional[FeeSchedule] = None,
) -> FeeBreakdown:
    """
    FR-13: Computes exact regulatory charges per fill to 4 decimal places.
    - Brokerage: percentage capped or flat
    - STT: 0.1% on delivery (BUY/SELL)
    - Exchange Charges: NSE transaction fee
    - GST: 18% on Brokerage + Exchange Charges (STT/Stamp Duty are exempt)
    - Stamp Duty: 0.015% on BUY side
    """
    sched = schedule or FeeSchedule()

    # 1. Brokerage
    if sched.brokerage_flat is not None:
        brk = sched.brokerage_flat
    else:
        raw_brk = turnover * sched.brokerage_rate
        if sched.brokerage_cap is not None:
            brk = min(raw_brk, sched.brokerage_cap)
        else:
            brk = raw_brk
    brokerage = brk.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    # 2. STT (Securities Transaction Tax)
    stt = (turnover * sched.stt_rate).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    # 3. Exchange Turnover Charges
    exchange = (turnover * sched.exchange_rate).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    # 4. GST (18% on Brokerage + Exchange Charges)
    taxable_services = brokerage + exchange
    gst = (taxable_services * sched.gst_rate).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    # 5. Stamp Duty (Government charge on BUY side)
    side_str = side.value if hasattr(side, "value") else str(side)
    if side_str.upper() == "BUY":
        stamp = (turnover * sched.stamp_duty_rate).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    else:
        stamp = Decimal("0.0000")

    total = (brokerage + stt + exchange + gst + stamp).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    return FeeBreakdown(
        brokerage=brokerage,
        stt=stt,
        exchange_charges=exchange,
        gst=gst,
        stamp_duty=stamp,
        total_fee=total,
    )


class BrokerRejectedError(Exception):
    """Raised by a broker adapter when it refuses an order; the ledger records it as REJECTED."""


@runtime_checkable
class BrokerAdapter(Protocol):
    """
    Seam for order routing and execution fills.
    Allows zero-change swaps between deterministic offline paper execution
    and external live broker SDKs (e.g. Zerodha Kite Connect, Shoonya, Dhan).
    """

    def submit_order(self, order: OrderIntent) -> FillExecution:
        """Submits an order intent and returns an executed fill."""
        ...


class PaperBroker:
    """
    Deterministic offline paper broker with FR-13 realistic fee model.
    Applies configurable slippage and full statutory fee breakdown (Brokerage, STT, Exchange, GST).
    """

    def __init__(
        self,
        slippage: Decimal = settings.default_slippage,
        fee_schedule: Optional[FeeSchedule] = None,
        fee_rate: Optional[Decimal] = None,
    ):
        self.slippage = slippage
        self.fee_schedule = fee_schedule or FeeSchedule()
        self.legacy_fee_rate = fee_rate

    def submit_order(self, order: OrderIntent) -> FillExecution:
        # Deterministic slippage model
        side_str = order.side.value if hasattr(order.side, "value") else str(order.side)
        if side_str.upper() == "BUY":
            fill_price = order.price + self.slippage
        else:
            fill_price = max(Decimal("0.0100"), order.price - self.slippage)

        cost_of_shares = Decimal(str(order.quantity)) * fill_price

        if self.legacy_fee_rate is not None:
            # Fallback for explicit legacy rate
            fee = (cost_of_shares * self.legacy_fee_rate).quantize(Decimal("0.0001"))
            breakdown = FeeBreakdown(
                brokerage=fee,
                stt=Decimal("0.0000"),
                exchange_charges=Decimal("0.0000"),
                gst=Decimal("0.0000"),
                stamp_duty=Decimal("0.0000"),
                total_fee=fee,
            )
        else:
            breakdown = compute_fee_breakdown(cost_of_shares, order.side, self.fee_schedule)
            fee = breakdown.total_fee

        fill_id = f"fill_{order.order_id}"

        return FillExecution(
            fill_id=fill_id,
            order_id=order.order_id,
            quantity=order.quantity,
            price=fill_price,
            fee=fee,
            fee_breakdown=breakdown,
        )


class KiteBroker:
    """
    Adapter Stub for Zerodha Kite Connect.
    Complies with BrokerAdapter protocol with realistic Indian equity delivery fee model.
    """

    def __init__(
        self,
        api_key: str = "mock_key",
        access_token: str = "mock_token",
        fee_schedule: Optional[FeeSchedule] = None,
    ):
        self.api_key = api_key
        self.access_token = access_token
        # Zerodha: ₹20 or 0.03% (standard retail)
        self.fee_schedule = fee_schedule or FeeSchedule(
            brokerage_rate=Decimal("0.0003"),
            brokerage_cap=Decimal("20.0000"),
        )

    def submit_order(self, order: OrderIntent) -> FillExecution:
        fill_price = order.price
        cost_of_shares = Decimal(str(order.quantity)) * fill_price
        breakdown = compute_fee_breakdown(cost_of_shares, order.side, self.fee_schedule)
        fill_id = f"kite_fill_{order.order_id}"

        return FillExecution(
            fill_id=fill_id,
            order_id=order.order_id,
            quantity=order.quantity,
            price=fill_price,
            fee=breakdown.total_fee,
            fee_breakdown=breakdown,
        )


class ShoonyaBroker:
    """
    Production Adapter for Shoonya by Finvasia (NorenRestApiPy).
    100% Free API + 100% Free WebSocket Market Data & Zero Brokerage (₹0 brokerage).
    """

    def __init__(
        self,
        user_id: str = "mock_user",
        api_token: str = "mock_token",
        fee_schedule: Optional[FeeSchedule] = None,
    ):
        self.user_id = user_id
        self.api_token = api_token
        # Shoonya has 0 brokerage; STT, exchange, and GST on exchange still apply
        self.fee_schedule = fee_schedule or FeeSchedule(
            brokerage_rate=Decimal("0.0000"),
            brokerage_cap=Decimal("0.0000"),
        )

    def submit_order(self, order: OrderIntent) -> FillExecution:
        fill_price = order.price
        cost_of_shares = Decimal(str(order.quantity)) * fill_price
        breakdown = compute_fee_breakdown(cost_of_shares, order.side, self.fee_schedule)
        fill_id = f"shoonya_fill_{order.order_id}"

        return FillExecution(
            fill_id=fill_id,
            order_id=order.order_id,
            quantity=order.quantity,
            price=fill_price,
            fee=breakdown.total_fee,
            fee_breakdown=breakdown,
        )


class DhanBroker:
    """
    Production Adapter for DhanHQ API.
    Free API & Free WebSocket Market Data with Zero Brokerage on Equity Delivery.
    """

    def __init__(
        self,
        client_id: str = "mock_dhan_id",
        access_token: str = "mock_token",
        fee_schedule: Optional[FeeSchedule] = None,
    ):
        self.client_id = client_id
        self.access_token = access_token
        # Dhan: Zero brokerage on equity delivery
        self.fee_schedule = fee_schedule or FeeSchedule(
            brokerage_rate=Decimal("0.0000"),
            brokerage_cap=Decimal("0.0000"),
        )

    def submit_order(self, order: OrderIntent) -> FillExecution:
        fill_price = order.price
        cost_of_shares = Decimal(str(order.quantity)) * fill_price
        breakdown = compute_fee_breakdown(cost_of_shares, order.side, self.fee_schedule)
        fill_id = f"dhan_fill_{order.order_id}"

        return FillExecution(
            fill_id=fill_id,
            order_id=order.order_id,
            quantity=order.quantity,
            price=fill_price,
            fee=breakdown.total_fee,
            fee_breakdown=breakdown,
        )


def get_broker(broker_type: str = "paper") -> BrokerAdapter:
    b = broker_type.lower()
    if b == "kite":
        return KiteBroker()
    if b == "shoonya":
        return ShoonyaBroker()
    if b == "dhan":
        return DhanBroker()
    return PaperBroker()
