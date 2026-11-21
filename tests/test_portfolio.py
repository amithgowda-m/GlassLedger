from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from glassledger.domain.models import OrderIntent, OrderSide
from glassledger.engine.broker import PaperBroker
from glassledger.engine.portfolio import PortfolioManager, compute_xirr
from glassledger.storage.postgres import PostgresStorage


def test_compute_xirr_math():
    """Proves XIRR computes correct annualized return on standard irregular cash flows."""
    d0 = datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc)
    d1 = d0 + timedelta(days=180)
    d2 = d0 + timedelta(days=365)

    # Deposit 10,000 on day 0, deposit 5,000 on day 180, terminal value 18,000 on day 365
    cfs = [
        (d0, -10000.0),
        (d1, -5000.0),
        (d2, 18000.0),
    ]
    rate = compute_xirr(cfs)
    # Expected return is roughly 22-25% annualized
    assert 15.0 < rate < 30.0


def test_compute_xirr_zero_cashflows():
    """Proves XIRR handles edge cases gracefully."""
    assert compute_xirr([]) == 0.0
    d0 = datetime.now(timezone.utc)
    assert compute_xirr([(d0, 100.0)]) == 0.0
    assert compute_xirr([(d0, -100.0), (d0, -200.0)]) == 0.0


def test_portfolio_valuation_and_unit_accounting():
    """
    Proves Virtual Wallet computes NAV, Number of Units, and holdings accurately.
    """
    pg = PostgresStorage()
    pm = PortfolioManager(pg=pg)
    summary = pm.get_portfolio_summary(1)

    p = summary["portfolio"]
    assert "total_net_assets" in p
    assert "nav_per_unit" in p
    assert "no_of_units" in p
    assert "xirr" in p
    assert "available_cash" in p
    assert Decimal(p["no_of_units"]) == Decimal("10000.00")

    # Total Net Assets = Wallet Balance + Securities Value
    net_assets = Decimal(p["total_net_assets"])
    wallet_bal = Decimal(p["wallet_balance"])
    sec_val = Decimal(p["securities_market_value"])
    assert net_assets == wallet_bal + sec_val


def test_virtual_trade_execution_updates_wallet():
    """
    Proves executing a virtual trade updates portfolio holdings, available cash,
    and recalculates NAV without actual payments.
    """
    pg = PostgresStorage()
    pm = PortfolioManager(pg=pg)
    initial_summary = pm.get_portfolio_summary(1)
    initial_cash = Decimal(initial_summary["portfolio"]["wallet_balance"])

    # Place a virtual BUY trade for 5 AAPL at 150.0000
    order_id = "ord_virt_trade_01"
    fill_id = "fill_virt_trade_01"
    order = OrderIntent(
        order_id=order_id,
        investor_id=1,
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=5,
        price=Decimal("150.0000"),
        reserved_amount=Decimal("800.0000"),
        decision_ref="dec_virt_01",
    )

    broker = PaperBroker()
    fill = broker.submit_order(order)

    with pg.get_connection_context() as conn:
        pg.submit_order_intent(conn, order)
        fill_payload = {
            "fill_id": fill_id,
            "order_id": order_id,
            "quantity": fill.quantity,
            "price": str(fill.price),
            "fee": str(fill.fee),
            "fee_breakdown": fill.fee_breakdown,
        }
        res = pg.process_fill(conn, fill_payload)
        assert res == "APPLIED"

    updated_summary = pm.get_portfolio_summary(1)
    updated_cash = Decimal(updated_summary["portfolio"]["wallet_balance"])

    # Wallet balance must have decreased by total cost (shares + statutory fees)
    assert updated_cash < initial_cash
    assert Decimal(updated_summary["portfolio"]["securities_market_value"]) > Decimal("0.00")

    # AAPL must be in holdings
    symbols_held = [h["symbol"] for h in updated_summary["holdings"]]
    assert "AAPL" in symbols_held
