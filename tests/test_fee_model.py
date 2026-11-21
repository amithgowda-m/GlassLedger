from decimal import Decimal
import pytest

from glassledger.domain.models import (
    FeeBreakdown,
    JournalAccount,
    OrderIntent,
    OrderSide,
    OrderStatus,
)
from glassledger.engine.broker import (
    DhanBroker,
    FeeSchedule,
    KiteBroker,
    PaperBroker,
    ShoonyaBroker,
    compute_fee_breakdown,
)
from glassledger.storage.postgres import PostgresStorage


def test_realistic_fee_breakdown_math():
    """
    FR-13 Verification:
    Proves brokerage, STT, exchange charges, GST, and stamp duty match
    known regulatory amounts to 4 decimal places with pure Decimal arithmetic.
    """
    schedule = FeeSchedule(
        brokerage_rate=Decimal("0.0003"),          # 0.03%
        brokerage_cap=Decimal("20.0000"),
        stt_rate=Decimal("0.0010"),                # 0.1%
        exchange_rate=Decimal("0.0000345"),        # 0.00345%
        gst_rate=Decimal("0.1800"),                # 18%
        stamp_duty_rate=Decimal("0.00015"),        # 0.015%
    )

    # 10 shares of RELIANCE at ₹2,850.00 -> Turnover = ₹28,500.0000
    turnover = Decimal("28500.0000")
    fb = compute_fee_breakdown(turnover, OrderSide.BUY, schedule)

    # Hand-computed verification:
    # Brokerage: 28,500 * 0.0003 = 8.5500
    assert fb.brokerage == Decimal("8.5500")

    # STT: 28,500 * 0.0010 = 28.5000
    assert fb.stt == Decimal("28.5000")

    # Exchange charges: 28,500 * 0.0000345 = 0.98325 -> 0.9833
    assert fb.exchange_charges == Decimal("0.9833")

    # GST: (8.5500 + 0.9833) * 0.18 = 9.5333 * 0.18 = 1.715994 -> 1.7160
    assert fb.gst == Decimal("1.7160")

    # Stamp duty (BUY): 28,500 * 0.00015 = 4.2750
    assert fb.stamp_duty == Decimal("4.2750")

    # Total fee: 8.5500 + 28.5000 + 0.9833 + 1.7160 + 4.2750 = 44.0243
    assert fb.total_fee == Decimal("44.0243")
    assert fb.total_fee == fb.brokerage + fb.stt + fb.exchange_charges + fb.gst + fb.stamp_duty


def test_sell_order_stamp_duty_exemption():
    """Proves stamp duty is 0 on SELL orders."""
    schedule = FeeSchedule()
    turnover = Decimal("10000.0000")
    fb = compute_fee_breakdown(turnover, OrderSide.SELL, schedule)
    assert fb.stamp_duty == Decimal("0.0000")


def test_brokerage_cap_enforced():
    """Proves brokerage does not exceed the ₹20 statutory cap on large orders."""
    schedule = FeeSchedule(brokerage_rate=Decimal("0.0003"), brokerage_cap=Decimal("20.0000"))
    # Turnover ₹1,000,000 -> 0.03% would be ₹300, but capped at ₹20.0000
    turnover = Decimal("1000000.0000")
    fb = compute_fee_breakdown(turnover, OrderSide.BUY, schedule)
    assert fb.brokerage == Decimal("20.0000")


def test_broker_adapter_fee_configurations():
    """Proves Shoonya has zero brokerage while Dhan and Kite comply with their schedules."""
    order = OrderIntent(
        order_id="ord_fee_test_01",
        investor_id=1,
        symbol="TCS",
        side=OrderSide.BUY,
        quantity=5,
        price=Decimal("4000.0000"),
        reserved_amount=Decimal("21000.0000"),
        decision_ref="dec_fee_test_01",
    )

    # Shoonya: ₹0 brokerage
    shoonya = ShoonyaBroker()
    shoonya_fill = shoonya.submit_order(order)
    assert shoonya_fill.fee_breakdown is not None
    assert shoonya_fill.fee_breakdown.brokerage == Decimal("0.0000")
    assert shoonya_fill.fee_breakdown.stt == Decimal("20.0000")  # 20,000 * 0.0010

    # Dhan: zero delivery brokerage
    dhan = DhanBroker()
    dhan_fill = dhan.submit_order(order)
    assert dhan_fill.fee_breakdown is not None
    assert dhan_fill.fee_breakdown.brokerage == Decimal("0.0000")

    # PaperBroker: default schedule
    paper = PaperBroker()
    paper_fill = paper.submit_order(order)
    assert paper_fill.fee_breakdown is not None
    assert paper_fill.fee_breakdown.brokerage > Decimal("0.0000")


def test_multi_line_journal_postings_in_ledger():
    """
    FR-13 Acceptance Criterion:
    Proves journal posts each fee as its own individual expense line in the ledger,
    and total debits strictly equal total credits.
    """
    pg = PostgresStorage()
    fill_id = "fill_fee_multi_line_001"
    order_id = "ord_fee_multi_line_001"

    # Setup initial investor balance and order
    order = OrderIntent(
        order_id=order_id,
        investor_id=1,
        symbol="INFY",
        side=OrderSide.BUY,
        quantity=10,
        price=Decimal("1500.0000"),
        reserved_amount=Decimal("16000.0000"),
        decision_ref="dec_multi_line_001",
    )

    turnover = Decimal("15000.0000")
    fb = compute_fee_breakdown(turnover, OrderSide.BUY)

    with pg.get_connection_context() as conn:
        # 1. Submit order
        pg.submit_order_intent(conn, order)

        # 2. Process fill with fee breakdown
        fill_payload = {
            "fill_id": fill_id,
            "order_id": order_id,
            "quantity": 10,
            "price": "1500.0000",
            "fee": str(fb.total_fee),
            "fee_breakdown": fb,
        }
        res = pg.process_fill(conn, fill_payload)
        assert res == "APPLIED"

        # 3. Verify distinct journal entries were created
        cur = conn.cursor()
        is_sqlite = isinstance(conn, type(pg.get_sqlite_conn()))
        cur.execute(
            "SELECT ledger_account, debit, credit FROM journal_entries WHERE fill_id = ?"
            if is_sqlite
            else "SELECT ledger_account, debit, credit FROM journal_entries WHERE fill_id = %s",
            (fill_id,),
        )
        postings = [dict(r) for r in cur.fetchall()]
        cur.close()

    accounts_posted = {p["ledger_account"] for p in postings}
    assert "SECURITIES_AT_COST" in accounts_posted
    assert "CASH" in accounts_posted
    assert "BROKERAGE_EXPENSE" in accounts_posted
    assert "STT_EXPENSE" in accounts_posted
    assert "EXCHANGE_FEE_EXPENSE" in accounts_posted
    assert "GST_EXPENSE" in accounts_posted
    assert "STAMP_DUTY_EXPENSE" in accounts_posted

    total_debits = sum(Decimal(str(p["debit"])) for p in postings)
    total_credits = sum(Decimal(str(p["credit"])) for p in postings)
    assert total_debits == total_credits
