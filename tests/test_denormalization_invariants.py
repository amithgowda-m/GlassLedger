"""
Reconciliation invariants for the deliberately denormalized columns documented in
docs/normalization.md ("Deliberate controlled denormalization").

Each stored value is recomputed from the immutable facts (orders, fills, journal_entries,
capital_events)
for a freshly created investor, so the test is independent of state left by other tests.
Runs on whichever backend is active (PostgreSQL or the SQLite fallback).

Note: the SQLite fallback stores NUMERIC as REAL, so comparisons are quantized to 4 dp.
"""
from decimal import Decimal
import sqlite3
import uuid

import pytest

from glassledger.domain.models import OrderIntent, OrderSide
from glassledger.engine.broker import compute_fee_breakdown
from glassledger.storage.postgres import PostgresStorage

Q = Decimal("0.0001")
OPENING_BALANCE = Decimal("500000.0000")


def _d(v) -> Decimal:
    return Decimal(str(v if v is not None else "0")).quantize(Q)


def _exec(conn, sql: str, params: tuple = ()) -> list[dict]:
    if isinstance(conn, sqlite3.Connection):
        sql = sql.replace("%s", "?")
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()] if cur.description else []
    finally:
        cur.close()


@pytest.fixture(scope="module")
def scenario():
    """
    Fresh investor, then: BUY 10 @ 1500, BUY 5 @ 1600, SELL 6 @ 1700 (all filled),
    plus one BUY and one SELL left PENDING (reserved, never filled).
    """
    pg = PostgresStorage()
    uid = uuid.uuid4().hex[:8]
    investor_id = 700000 + int(uid[:5], 16) % 100000
    symbol = "INFY"

    pg.create_investor(
        name="Invariant Test", email=f"inv_{uid}@test.local",
        opening_balance=OPENING_BALANCE, investor_id=investor_id,
    )

    trades = [
        (OrderSide.BUY, 10, Decimal("1500.0000")),
        (OrderSide.BUY, 5, Decimal("1600.0000")),
        (OrderSide.SELL, 6, Decimal("1700.0000")),
    ]
    for i, (side, qty, price) in enumerate(trades):
        turnover = Decimal(qty) * price
        fb = compute_fee_breakdown(turnover, side)
        reserved = (turnover + fb.total_fee + Decimal("10.0000")) if side == OrderSide.BUY else Decimal("0.0000")
        order = OrderIntent(
            order_id=f"ord_inv_{uid}_{i}",
            investor_id=investor_id,
            symbol=symbol,
            side=side,
            quantity=qty,
            price=price,
            reserved_amount=reserved,
            decision_ref=f"dec_inv_{uid}_{i}",
        )
        with pg.get_connection_context() as conn:
            pg.submit_order_intent(conn, order)
            res = pg.process_fill(
                conn,
                {
                    "fill_id": f"fill_inv_{uid}_{i}",
                    "order_id": order.order_id,
                    "quantity": qty,
                    "price": str(price),
                    "fee": str(fb.total_fee),
                    "fee_breakdown": fb,
                },
            )
            assert res == "APPLIED"

    pending = OrderIntent(
        order_id=f"ord_inv_{uid}_pending",
        investor_id=investor_id,
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=3,
        price=Decimal("1650.0000"),
        reserved_amount=Decimal("4999.9900"),
        decision_ref=f"dec_inv_{uid}_pending",
    )
    with pg.get_connection_context() as conn:
        pg.submit_order_intent(conn, pending)

    pending_sell = OrderIntent(
        order_id=f"ord_inv_{uid}_pending_sell",
        investor_id=investor_id,
        symbol=symbol,
        side=OrderSide.SELL,
        quantity=2,
        price=Decimal("1700.0000"),
        reserved_amount=Decimal("0.0000"),
        decision_ref=f"dec_inv_{uid}_pending_sell",
    )
    with pg.get_connection_context() as conn:
        pg.submit_order_intent(conn, pending_sell)

    return {"pg": pg, "investor_id": investor_id, "symbol": symbol}


def test_wallet_balance_equals_cash_journal_exactly(scenario):
    """wallet_balance == SUM(CASH.debit - CASH.credit) over the investor's fills AND capital events."""
    pg, inv = scenario["pg"], scenario["investor_id"]
    with pg.get_connection_context() as conn:
        wallet = _exec(conn, "SELECT wallet_balance FROM investors WHERE investor_id = %s", (inv,))[0]
        cash = _exec(
            conn,
            """
            SELECT COALESCE(SUM(j.debit), 0) AS d, COALESCE(SUM(j.credit), 0) AS c
            FROM journal_entries j
            LEFT JOIN fills f  ON f.fill_id = j.fill_id
            LEFT JOIN orders o ON o.order_id = f.order_id
            LEFT JOIN capital_events ce ON ce.event_id = j.capital_event_id
            WHERE j.ledger_account = 'CASH' AND COALESCE(o.investor_id, ce.investor_id) = %s
            """,
            (inv,),
        )[0]
    assert _d(wallet["wallet_balance"]) == _d(_d(cash["d"]) - _d(cash["c"]))


def test_reserved_cash_equals_pending_buy_reservations(scenario):
    """reserved_cash == SUM(reserved_amount) of the investor's PENDING BUY orders."""
    pg, inv = scenario["pg"], scenario["investor_id"]
    with pg.get_connection_context() as conn:
        stored = _exec(conn, "SELECT reserved_cash FROM investors WHERE investor_id = %s", (inv,))[0]
        derived = _exec(
            conn,
            "SELECT COALESCE(SUM(reserved_amount), 0) AS r FROM orders "
            "WHERE investor_id = %s AND side = 'BUY' AND status = 'PENDING'",
            (inv,),
        )[0]
    assert _d(stored["reserved_cash"]) == _d(derived["r"])
    assert _d(stored["reserved_cash"]) == Decimal("4999.9900")


def test_holding_quantity_equals_signed_fill_sum(scenario):
    """holdings.quantity == SUM(BUY fill qty) - SUM(SELL fill qty) for (investor, symbol)."""
    pg, inv, sym = scenario["pg"], scenario["investor_id"], scenario["symbol"]
    with pg.get_connection_context() as conn:
        h = _exec(
            conn,
            "SELECT quantity, reserved_quantity FROM holdings WHERE investor_id = %s AND symbol = %s",
            (inv, sym),
        )[0]
        s = _exec(
            conn,
            """
            SELECT COALESCE(SUM(CASE WHEN o.side = 'BUY' THEN f.quantity ELSE -f.quantity END), 0) AS q
            FROM fills f JOIN orders o ON o.order_id = f.order_id
            WHERE o.investor_id = %s AND o.symbol = %s
            """,
            (inv, sym),
        )[0]
    assert int(h["quantity"]) == int(s["q"]) == 9


def test_reserved_quantity_equals_pending_sell_quantity(scenario):
    """holdings.reserved_quantity == SUM(quantity) of the investor's PENDING SELL orders for the symbol."""
    pg, inv, sym = scenario["pg"], scenario["investor_id"], scenario["symbol"]
    with pg.get_connection_context() as conn:
        h = _exec(conn, "SELECT reserved_quantity FROM holdings WHERE investor_id = %s AND symbol = %s", (inv, sym))[0]
        p = _exec(
            conn,
            "SELECT COALESCE(SUM(quantity), 0) AS q FROM orders "
            "WHERE investor_id = %s AND symbol = %s AND side = 'SELL' AND status = 'PENDING'",
            (inv, sym),
        )[0]
    assert int(h["reserved_quantity"]) == int(p["q"]) == 2


def test_avg_buy_price_is_fee_exclusive_weighted_average_of_buys(scenario):
    """With buys before sells, avg_buy_price == SUM(qty*price)/SUM(qty) over BUY fills (fees excluded)."""
    pg, inv, sym = scenario["pg"], scenario["investor_id"], scenario["symbol"]
    with pg.get_connection_context() as conn:
        h = _exec(conn, "SELECT avg_buy_price FROM holdings WHERE investor_id = %s AND symbol = %s", (inv, sym))[0]
        buys = _exec(
            conn,
            "SELECT f.quantity, f.price FROM fills f JOIN orders o ON o.order_id = f.order_id "
            "WHERE o.investor_id = %s AND o.symbol = %s AND o.side = 'BUY'",
            (inv, sym),
        )
    notional = sum(Decimal(int(b["quantity"])) * _d(b["price"]) for b in buys)
    qty = sum(int(b["quantity"]) for b in buys)
    assert _d(h["avg_buy_price"]) == (notional / qty).quantize(Q)


def test_order_status_filled_iff_fill_exists(scenario):
    """orders.status = 'FILLED' <=> at least one fills row references the order."""
    pg, inv = scenario["pg"], scenario["investor_id"]
    with pg.get_connection_context() as conn:
        rows = _exec(
            conn,
            """
            SELECT o.order_id, o.status, COUNT(f.fill_id) AS n_fills
            FROM orders o LEFT JOIN fills f ON f.order_id = o.order_id
            WHERE o.investor_id = %s
            GROUP BY o.order_id, o.status
            """,
            (inv,),
        )
    assert len(rows) == 5
    for r in rows:
        assert (r["status"] == "FILLED") == (int(r["n_fills"]) > 0), r


def test_every_fill_journal_is_balanced(scenario):
    """For every fill: SUM(debit) == SUM(credit) across its journal lines."""
    pg, inv = scenario["pg"], scenario["investor_id"]
    with pg.get_connection_context() as conn:
        rows = _exec(
            conn,
            """
            SELECT j.fill_id, SUM(j.debit) AS d, SUM(j.credit) AS c
            FROM journal_entries j
            JOIN fills f  ON f.fill_id = j.fill_id
            JOIN orders o ON o.order_id = f.order_id
            WHERE o.investor_id = %s
            GROUP BY j.fill_id
            """,
            (inv,),
        )
    assert len(rows) == 3
    for r in rows:
        assert _d(r["d"]) == _d(r["c"]), r


def test_fill_fee_equals_sum_of_expense_journal_lines(scenario):
    """fills.fee == SUM(debit) of that fill's *_EXPENSE journal lines (redundant copy of the fee)."""
    pg, inv = scenario["pg"], scenario["investor_id"]
    with pg.get_connection_context() as conn:
        rows = _exec(
            conn,
            """
            SELECT f.fill_id, f.fee, COALESCE(SUM(j.debit), 0) AS expense
            FROM fills f
            JOIN orders o ON o.order_id = f.order_id
            LEFT JOIN journal_entries j
                   ON j.fill_id = f.fill_id AND j.ledger_account LIKE '%%EXPENSE'
            WHERE o.investor_id = %s
            GROUP BY f.fill_id, f.fee
            """,
            (inv,),
        )
    assert len(rows) == 3
    for r in rows:
        assert _d(r["fee"]) == _d(r["expense"]), r
