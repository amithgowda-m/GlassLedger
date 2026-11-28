"""
Settlement-correctness tests (SELL validation, share reservation, SELL accounting,
opening balance, order lifecycle, decision_ref uniqueness).

Each test creates its own investor so tests are independent of each other and of seed data.
Assertions here check ledger *logic*; exact-decimal and concurrency guarantees are only
claimed for PostgreSQL and live in PG-only tests.
"""
from decimal import Decimal
import uuid

import pytest

from glassledger.domain.invariants import InsufficientHoldingsError, LedgerResetRefusedError
from glassledger.domain.models import OrderIntent, OrderSide
from glassledger.engine.broker import compute_fee_breakdown
from glassledger.storage.postgres import PostgresStorage, _Cursor

Q = Decimal("0.0001")


@pytest.fixture
def pg() -> PostgresStorage:
    return PostgresStorage()


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def new_investor(pg: PostgresStorage, cash: str = "100000.0000") -> int:
    uid = _uid()
    return pg.create_investor(name="Settlement Test", email=f"s_{uid}@test.local", opening_balance=Decimal(cash))


def make_order(investor_id: int, side: OrderSide, qty: int, price: str, symbol: str = "INFY") -> OrderIntent:
    uid = _uid()
    p = Decimal(price)
    reserved = (Decimal(qty) * p * Decimal("1.01")).quantize(Q) if side == OrderSide.BUY else Decimal("0")
    return OrderIntent(
        order_id=f"ord_t_{uid}", investor_id=investor_id, symbol=symbol, side=side,
        quantity=qty, price=p, reserved_amount=reserved, decision_ref=f"dec_t_{uid}",
    )


def fill_payload(order: OrderIntent, price: str | None = None) -> dict:
    p = Decimal(price) if price else order.price
    fb = compute_fee_breakdown(Decimal(order.quantity) * p, order.side)
    return {
        "fill_id": f"fill_{order.order_id}", "order_id": order.order_id, "quantity": order.quantity,
        "price": str(p), "fee": str(fb.total_fee), "fee_breakdown": fb,
    }


def trade(pg: PostgresStorage, investor_id: int, side: OrderSide, qty: int, price: str, symbol: str = "INFY") -> OrderIntent:
    order = make_order(investor_id, side, qty, price, symbol)
    with pg.get_connection_context() as conn:
        with pg.transaction(conn):
            pg.submit_order_intent(conn, order)
            assert pg.process_fill(conn, fill_payload(order)) == "APPLIED"
    return order


def account(pg: PostgresStorage, investor_id: int) -> tuple[Decimal, Decimal]:
    inv = pg.get_investor_account(investor_id)
    return Decimal(str(inv["wallet_balance"])).quantize(Q), Decimal(str(inv["reserved_cash"])).quantize(Q)


def holding(pg: PostgresStorage, investor_id: int, symbol: str = "INFY") -> dict | None:
    rows = pg._query(
        "SELECT quantity, reserved_quantity, avg_buy_price FROM holdings WHERE investor_id = %s AND symbol = %s",
        (investor_id, symbol),
    )
    return rows[0] if rows else None


def order_row(pg: PostgresStorage, order_id: str) -> dict | None:
    rows = pg._query("SELECT * FROM orders WHERE order_id = %s", (order_id,))
    return rows[0] if rows else None


# ---------------------------------------------------------------- Step 1: SELL validation

def test_sell_without_holding_is_rejected_with_no_side_effects(pg):
    inv = new_investor(pg)
    before = account(pg, inv)
    order = make_order(inv, OrderSide.SELL, 5, "1500.0000")

    with pytest.raises(InsufficientHoldingsError, match="INSUFFICIENT_HOLDINGS"):
        with pg.get_connection_context() as conn:
            with pg.transaction(conn):
                pg.submit_order_intent(conn, order)

    assert account(pg, inv) == before
    assert order_row(pg, order.order_id) is None
    assert holding(pg, inv) is None


def test_sell_more_than_held_is_rejected_with_no_side_effects(pg):
    inv = new_investor(pg)
    trade(pg, inv, OrderSide.BUY, 5, "1500.0000")
    before_acct, before_hold = account(pg, inv), holding(pg, inv)
    order = make_order(inv, OrderSide.SELL, 6, "1500.0000")

    with pytest.raises(InsufficientHoldingsError):
        with pg.get_connection_context() as conn:
            with pg.transaction(conn):
                pg.submit_order_intent(conn, order)

    assert account(pg, inv) == before_acct
    assert holding(pg, inv) == before_hold
    assert order_row(pg, order.order_id) is None


def test_fill_of_sell_order_without_holding_is_rejected(pg):
    """Defence in depth: a SELL order that bypassed intake must still not settle."""
    inv = new_investor(pg)
    before = account(pg, inv)
    order = make_order(inv, OrderSide.SELL, 5, "1500.0000")
    with pg.get_connection_context() as conn:
        with _Cursor(conn) as c:
            c.run(
                "INSERT INTO orders (order_id, investor_id, symbol, side, quantity, price, reserved_amount, status, decision_ref) "
                "VALUES (%s, %s, 'INFY', 'SELL', 5, 1500, 0, 'PENDING', %s)",
                (order.order_id, inv, order.decision_ref),
            )

    with pytest.raises(InsufficientHoldingsError):
        with pg.get_connection_context() as conn:
            with pg.transaction(conn):
                pg.process_fill(conn, fill_payload(order))

    assert account(pg, inv) == before
    assert pg._query("SELECT 1 FROM fills WHERE order_id = %s", (order.order_id,)) == []


# ---------------------------------------------------------------- Step 2: share reservation

def submit(pg: PostgresStorage, order: OrderIntent) -> None:
    with pg.get_connection_context() as conn:
        with pg.transaction(conn):
            pg.submit_order_intent(conn, order)


def settle(pg: PostgresStorage, order: OrderIntent) -> str:
    with pg.get_connection_context() as conn:
        with pg.transaction(conn):
            return pg.process_fill(conn, fill_payload(order))


def test_sell_reserves_shares_at_intake_and_releases_on_fill(pg):
    inv = new_investor(pg)
    trade(pg, inv, OrderSide.BUY, 10, "1500.0000")

    first = make_order(inv, OrderSide.SELL, 4, "1550.0000")
    submit(pg, first)
    h = holding(pg, inv)
    assert (int(h["quantity"]), int(h["reserved_quantity"])) == (10, 4)

    # Only 6 unreserved shares remain, so a SELL of 7 must be refused.
    with pytest.raises(InsufficientHoldingsError):
        submit(pg, make_order(inv, OrderSide.SELL, 7, "1550.0000"))
    assert int(holding(pg, inv)["reserved_quantity"]) == 4

    assert settle(pg, first) == "APPLIED"
    h = holding(pg, inv)
    assert (int(h["quantity"]), int(h["reserved_quantity"])) == (6, 0)


def test_sell_fill_requires_reserved_shares(pg):
    """A SELL order that never reserved shares (bypassed intake) cannot settle."""
    inv = new_investor(pg)
    trade(pg, inv, OrderSide.BUY, 10, "1500.0000")
    order = make_order(inv, OrderSide.SELL, 5, "1500.0000")
    with pg.get_connection_context() as conn:
        with _Cursor(conn) as c:
            c.run(
                "INSERT INTO orders (order_id, investor_id, symbol, side, quantity, price, reserved_amount, status, decision_ref) "
                "VALUES (%s, %s, 'INFY', 'SELL', 5, 1500, 0, 'PENDING', %s)",
                (order.order_id, inv, order.decision_ref),
            )
    with pytest.raises(InsufficientHoldingsError, match="reserved"):
        settle(pg, order)
    assert int(holding(pg, inv)["quantity"]) == 10


def test_holding_solvency_check_rejects_over_reservation(pg):
    """The DB itself refuses reserved_quantity > quantity (ck_holding_solvency on PostgreSQL)."""
    import sqlite3
    import psycopg

    inv = new_investor(pg)
    trade(pg, inv, OrderSide.BUY, 10, "1500.0000")
    with pytest.raises((sqlite3.IntegrityError, psycopg.IntegrityError)):
        with pg.get_connection_context() as conn:
            with _Cursor(conn) as c:
                c.run("UPDATE holdings SET reserved_quantity = 11 WHERE investor_id = %s AND symbol = 'INFY'", (inv,))


# ---------------------------------------------------------------- Step 3: SELL accounting

def journal_for(pg: PostgresStorage, fill_id: str) -> dict[str, tuple[Decimal, Decimal]]:
    rows = pg._query("SELECT ledger_account, debit, credit FROM journal_entries WHERE fill_id = %s", (fill_id,))
    return {r["ledger_account"]: (Decimal(str(r["debit"])).quantize(Q), Decimal(str(r["credit"])).quantize(Q)) for r in rows}


def realized_lines_total(pg: PostgresStorage, investor_id: int) -> Decimal:
    row = pg._query(
        """
        SELECT COALESCE(SUM(j.credit), 0) AS c, COALESCE(SUM(j.debit), 0) AS d
        FROM journal_entries j JOIN fills f ON f.fill_id = j.fill_id JOIN orders o ON o.order_id = f.order_id
        WHERE o.investor_id = %s AND j.ledger_account = 'REALIZED_GAIN_LOSS'
        """,
        (investor_id,),
    )[0]
    return (Decimal(str(row["c"])) - Decimal(str(row["d"]))).quantize(Q)


def test_sell_at_gain_credits_cost_basis_and_realized_gain(pg):
    inv = new_investor(pg)
    trade(pg, inv, OrderSide.BUY, 10, "1500.0000")
    trade(pg, inv, OrderSide.BUY, 10, "1600.0000")
    assert Decimal(str(holding(pg, inv)["avg_buy_price"])).quantize(Q) == Decimal("1550.0000")

    sell = trade(pg, inv, OrderSide.SELL, 5, "1700.0000")
    fee = Decimal(fill_payload(sell)["fee"])
    lines = journal_for(pg, f"fill_{sell.order_id}")

    assert lines["CASH"] == (Decimal("8500.0000") - fee, Decimal("0"))
    assert lines["SECURITIES_AT_COST"] == (Decimal("0"), Decimal("7750.0000"))  # 5 x avg 1550
    assert lines["REALIZED_GAIN_LOSS"] == (Decimal("0"), Decimal("750.0000"))
    assert sum(d for d, _ in lines.values()) == sum(c for _, c in lines.values())


def test_sell_at_loss_debits_realized_gain_loss(pg):
    inv = new_investor(pg)
    trade(pg, inv, OrderSide.BUY, 10, "1500.0000")
    sell = trade(pg, inv, OrderSide.SELL, 4, "1400.0000")
    lines = journal_for(pg, f"fill_{sell.order_id}")
    assert lines["SECURITIES_AT_COST"] == (Decimal("0"), Decimal("6000.0000"))
    assert lines["REALIZED_GAIN_LOSS"] == (Decimal("400.0000"), Decimal("0"))
    assert sum(d for d, _ in lines.values()) == sum(c for _, c in lines.values())


def test_avg_buy_price_and_cost_basis_round_half_up_to_4dp(pg):
    inv = new_investor(pg)
    trade(pg, inv, OrderSide.BUY, 1, "1.0000")
    trade(pg, inv, OrderSide.BUY, 2, "2.0000")      # avg = 5/3 -> 1.6667
    assert Decimal(str(holding(pg, inv)["avg_buy_price"])).quantize(Q) == Decimal("1.6667")
    sell = trade(pg, inv, OrderSide.SELL, 1, "3.0000")
    lines = journal_for(pg, f"fill_{sell.order_id}")
    assert lines["SECURITIES_AT_COST"] == (Decimal("0"), Decimal("1.6667"))
    assert lines["REALIZED_GAIN_LOSS"] == (Decimal("0"), Decimal("1.3333"))


def test_realized_pnl_equals_sum_of_realized_gain_loss_lines(pg):
    inv = new_investor(pg)
    trade(pg, inv, OrderSide.BUY, 10, "1500.0000")
    trade(pg, inv, OrderSide.SELL, 3, "1600.0000")   # +300
    trade(pg, inv, OrderSide.SELL, 2, "1450.0000")   # -100
    pnl = pg.get_pnl_summary(inv)
    assert Decimal(str(pnl["realized_pnl"])).quantize(Q) == realized_lines_total(pg, inv) == Decimal("200.0000")


def test_ledger_accounts_lookup_classifies_every_account(pg):
    rows = {r["account_code"]: r["account_type"] for r in pg._query("SELECT account_code, account_type FROM ledger_accounts")}
    assert rows["CASH"] == "ASSET"
    assert rows["SECURITIES_AT_COST"] == "ASSET"
    assert rows["REALIZED_GAIN_LOSS"] == "INCOME"
    assert rows["BROKERAGE_EXPENSE"] == "EXPENSE"


def test_journal_rejects_unknown_ledger_account(pg):
    import sqlite3
    import psycopg

    inv = new_investor(pg)
    buy = trade(pg, inv, OrderSide.BUY, 1, "100.0000")
    with pytest.raises((sqlite3.IntegrityError, psycopg.IntegrityError)):
        with pg.get_connection_context() as conn:
            with _Cursor(conn) as c:
                c.run(
                    "INSERT INTO journal_entries (entry_id, fill_id, ledger_account, debit, credit) "
                    "VALUES (%s, %s, 'NOT_AN_ACCOUNT', 1, 0)",
                    (f"j_bad_{_uid()}", f"fill_{buy.order_id}"),
                )


# ---------------------------------------------------------------- Step 4: opening balance

def cash_ledger_balance(pg: PostgresStorage, investor_id: int) -> Decimal:
    """SUM(CASH debit - credit) over every journal line attributable to the investor (fills + capital events)."""
    row = pg._query(
        """
        SELECT COALESCE(SUM(j.debit), 0) AS d, COALESCE(SUM(j.credit), 0) AS c
        FROM journal_entries j
        LEFT JOIN fills f ON f.fill_id = j.fill_id
        LEFT JOIN orders o ON o.order_id = f.order_id
        LEFT JOIN capital_events ce ON ce.event_id = j.capital_event_id
        WHERE j.ledger_account = 'CASH' AND COALESCE(o.investor_id, ce.investor_id) = %s
        """,
        (investor_id,),
    )[0]
    return (Decimal(str(row["d"])) - Decimal(str(row["c"]))).quantize(Q)


def test_create_investor_journals_opening_capital(pg):
    inv = pg.create_investor(name="Opening Test", email=f"o_{_uid()}@test.local", opening_balance=Decimal("250000.0000"))
    assert account(pg, inv) == (Decimal("250000.0000"), Decimal("0.0000"))
    lines = pg._query(
        """
        SELECT j.ledger_account, j.debit, j.credit, ce.event_type
        FROM journal_entries j JOIN capital_events ce ON ce.event_id = j.capital_event_id
        WHERE ce.investor_id = %s
        """,
        (inv,),
    )
    got = {r["ledger_account"]: (Decimal(str(r["debit"])).quantize(Q), Decimal(str(r["credit"])).quantize(Q)) for r in lines}
    assert got == {
        "CASH": (Decimal("250000.0000"), Decimal("0")),
        "OWNER_EQUITY": (Decimal("0"), Decimal("250000.0000")),
    }
    assert {r["event_type"] for r in lines} == {"OPENING_BALANCE"}
    assert cash_ledger_balance(pg, inv) == account(pg, inv)[0]


def test_cash_journal_equals_wallet_balance_exactly_after_trading(pg):
    inv = new_investor(pg, "300000.0000")
    trade(pg, inv, OrderSide.BUY, 10, "1500.0000")
    trade(pg, inv, OrderSide.BUY, 5, "1600.0000")
    trade(pg, inv, OrderSide.SELL, 6, "1700.0000")
    submit(pg, make_order(inv, OrderSide.BUY, 2, "1650.0000"))  # pending: reserves, moves no cash
    assert cash_ledger_balance(pg, inv) == account(pg, inv)[0]


def test_cash_reset_is_journaled_and_preserves_invariant(pg):
    inv = new_investor(pg, "100000.0000")
    trade(pg, inv, OrderSide.BUY, 10, "1500.0000")
    pg.reset_investor_cash(inv, Decimal("100000.0000"))
    assert account(pg, inv) == (Decimal("100000.0000"), Decimal("0.0000"))
    assert cash_ledger_balance(pg, inv) == Decimal("100000.0000")
    types = {r["event_type"] for r in pg._query("SELECT event_type FROM capital_events WHERE investor_id = %s", (inv,))}
    assert types == {"OPENING_BALANCE", "CAPITAL_ADJUSTMENT"}


def test_cash_reset_refused_while_orders_are_open(pg):
    inv = new_investor(pg)
    submit(pg, make_order(inv, OrderSide.BUY, 2, "1500.0000"))
    before = account(pg, inv)
    with pytest.raises(LedgerResetRefusedError, match="non-terminal"):
        pg.reset_investor_cash(inv, Decimal("100000.0000"))
    assert account(pg, inv) == before


# ---------------------------------------------------------------- Step 5: order lifecycle

from glassledger.domain.invariants import OrderStateError  # noqa: E402
from glassledger.engine.broker import BrokerRejectedError, PaperBroker  # noqa: E402


def cancel(pg: PostgresStorage, order_id: str, reason: str = "user cancel") -> str:
    with pg.get_connection_context() as conn:
        with pg.transaction(conn):
            return pg.cancel_order(conn, order_id, reason)


def test_cancel_buy_releases_cash_reservation(pg):
    inv = new_investor(pg)
    order = make_order(inv, OrderSide.BUY, 10, "1500.0000")
    submit(pg, order)
    assert account(pg, inv)[1] == order.reserved_amount

    assert cancel(pg, order.order_id) == "CANCELED"
    assert account(pg, inv) == (Decimal("100000.0000"), Decimal("0.0000"))
    row = order_row(pg, order.order_id)
    assert (row["status"], row["status_reason"]) == ("CANCELED", "user cancel")
    assert cancel(pg, order.order_id) == "ALREADY_CANCELED"   # idempotent
    assert account(pg, inv)[1] == Decimal("0.0000")


def test_cancel_sell_releases_share_reservation(pg):
    inv = new_investor(pg)
    trade(pg, inv, OrderSide.BUY, 10, "1500.0000")
    order = make_order(inv, OrderSide.SELL, 4, "1600.0000")
    submit(pg, order)
    assert int(holding(pg, inv)["reserved_quantity"]) == 4
    assert cancel(pg, order.order_id) == "CANCELED"
    h = holding(pg, inv)
    assert (int(h["quantity"]), int(h["reserved_quantity"])) == (10, 0)


def test_reject_releases_reservation_and_records_reason(pg):
    inv = new_investor(pg)
    order = make_order(inv, OrderSide.BUY, 10, "1500.0000")
    submit(pg, order)
    with pg.get_connection_context() as conn:
        with pg.transaction(conn):
            assert pg.reject_order(conn, order.order_id, "exchange closed") == "REJECTED"
    assert account(pg, inv)[1] == Decimal("0.0000")
    assert order_row(pg, order.order_id)["status"] == "REJECTED"


def test_filled_order_cannot_be_canceled(pg):
    inv = new_investor(pg)
    order = trade(pg, inv, OrderSide.BUY, 1, "100.0000")
    with pytest.raises(OrderStateError):
        cancel(pg, order.order_id)
    assert order_row(pg, order.order_id)["status"] == "FILLED"


def test_canceled_order_cannot_be_filled(pg):
    inv = new_investor(pg)
    order = make_order(inv, OrderSide.BUY, 10, "1500.0000")
    submit(pg, order)
    cancel(pg, order.order_id)
    before = account(pg, inv)
    with pytest.raises(OrderStateError, match="CANCELED"):
        settle(pg, order)
    assert account(pg, inv) == before
    assert pg._query("SELECT 1 FROM fills WHERE order_id = %s", (order.order_id,)) == []


def test_resubmitting_an_order_id_has_no_second_effect(pg):
    inv = new_investor(pg)
    order = make_order(inv, OrderSide.BUY, 10, "1500.0000")
    submit(pg, order)
    cancel(pg, order.order_id)
    submit(pg, order)  # must neither re-reserve nor resurrect the order
    assert account(pg, inv)[1] == Decimal("0.0000")
    assert order_row(pg, order.order_id)["status"] == "CANCELED"


def test_execute_order_records_validation_failure_as_rejected(pg):
    inv = new_investor(pg)
    order = make_order(inv, OrderSide.SELL, 5, "1500.0000")   # no holding
    result = pg.execute_order(order, PaperBroker())
    assert result["status"] == "REJECTED"
    assert "INSUFFICIENT_HOLDINGS" in result["reason"]
    row = order_row(pg, order.order_id)
    assert row["status"] == "REJECTED" and "INSUFFICIENT_HOLDINGS" in row["status_reason"]
    assert account(pg, inv) == (Decimal("100000.0000"), Decimal("0.0000"))


def test_execute_order_broker_rejection_releases_reservation_atomically(pg):
    class RejectingBroker:
        def submit_order(self, order):
            raise BrokerRejectedError("RMS: price band breached")

    inv = new_investor(pg)
    order = make_order(inv, OrderSide.BUY, 10, "1500.0000")
    result = pg.execute_order(order, RejectingBroker())
    assert result["status"] == "REJECTED"
    assert order_row(pg, order.order_id)["status"] == "REJECTED"
    assert account(pg, inv) == (Decimal("100000.0000"), Decimal("0.0000"))


def test_execute_order_is_idempotent_on_replay(pg):
    inv = new_investor(pg)
    order = make_order(inv, OrderSide.BUY, 2, "1500.0000")
    first = pg.execute_order(order, PaperBroker())
    after_first = account(pg, inv)
    second = pg.execute_order(order, PaperBroker())
    assert first["status"] == "FILLED" and second["status"] == "FILLED"
    assert account(pg, inv) == after_first


def test_crash_mid_cancel_rolls_back_atomically(pg):
    """Failure injected after the reservation release but before commit: nothing persists."""
    inv = new_investor(pg)
    order = make_order(inv, OrderSide.BUY, 10, "1500.0000")
    submit(pg, order)
    before = account(pg, inv)
    with pytest.raises(RuntimeError, match="CRASH_INJECTION"):
        with pg.get_connection_context() as conn:
            with pg.transaction(conn):
                pg.cancel_order(conn, order.order_id, "user cancel")
                raise RuntimeError("CRASH_INJECTION: process died before commit")
    assert account(pg, inv) == before
    assert order_row(pg, order.order_id)["status"] == "PENDING"


def test_seed_refuses_reset_while_investor_1_has_open_orders(pg):
    order = make_order(1, OrderSide.BUY, 1, "100.0000")
    submit(pg, order)
    try:
        with pytest.raises(LedgerResetRefusedError):
            pg.seed_reference_data()
    finally:
        cancel(pg, order.order_id)
    pg.seed_reference_data()
    assert account(pg, 1) == (Decimal("100000.0000"), Decimal("0.0000"))
    assert cash_ledger_balance(pg, 1) == Decimal("100000.0000")


# ---------------------------------------------------------------- Step 6: UNIQUE(decision_ref)

def test_second_order_for_same_decision_is_refused_atomically(pg):
    import sqlite3
    import psycopg

    inv = new_investor(pg)
    first = make_order(inv, OrderSide.BUY, 1, "1000.0000")
    submit(pg, first)
    reserved_after_first = account(pg, inv)[1]

    dup = make_order(inv, OrderSide.BUY, 1, "1000.0000")
    dup = dup.model_copy(update={"decision_ref": first.decision_ref})
    with pytest.raises((sqlite3.IntegrityError, psycopg.IntegrityError)):
        submit(pg, dup)

    assert order_row(pg, dup.order_id) is None
    assert account(pg, inv)[1] == reserved_after_first   # the duplicate's reservation rolled back


# ---------------------------------------------------------------- Migration 003: one line per account per source

def test_second_line_for_same_fill_and_account_is_refused(pg):
    import sqlite3
    import psycopg

    inv = new_investor(pg)
    buy = trade(pg, inv, OrderSide.BUY, 1, "100.0000")
    with pytest.raises((sqlite3.IntegrityError, psycopg.IntegrityError)):
        with pg.get_connection_context() as conn:
            with _Cursor(conn) as c:
                c.run(
                    "INSERT INTO journal_entries (entry_id, fill_id, ledger_account, debit, credit) "
                    "VALUES (%s, %s, 'CASH', 1, 0)",
                    (f"j_dup_{_uid()}", f"fill_{buy.order_id}"),
                )


def test_second_line_for_same_capital_event_and_account_is_refused(pg):
    import sqlite3
    import psycopg

    inv = new_investor(pg)
    event_id = pg._query("SELECT event_id FROM capital_events WHERE investor_id = %s", (inv,))[0]["event_id"]
    with pytest.raises((sqlite3.IntegrityError, psycopg.IntegrityError)):
        with pg.get_connection_context() as conn:
            with _Cursor(conn) as c:
                c.run(
                    "INSERT INTO journal_entries (entry_id, capital_event_id, ledger_account, debit, credit) "
                    "VALUES (%s, %s, 'CASH', 1, 0)",
                    (f"j_dup_{_uid()}", event_id),
                )
