from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import pytest

from glassledger.storage.postgres import PostgresStorage
from glassledger.domain.invariants import (
    SolvencyViolationError,
    UnbalancedJournalError,
    assert_account_solvency,
    assert_holding_solvency,
    assert_balanced_journal,
)
from glassledger.domain.models import JournalAccount, JournalPosting


def test_account_solvency_valid():
    """Proves valid solvency checks pass without error."""
    assert_account_solvency(Decimal("1000.0000"), Decimal("500.0000"))
    assert_account_solvency(Decimal("1000.0000"), Decimal("1000.0000"))
    assert_account_solvency(Decimal("0.0000"), Decimal("0.0000"))


def test_account_solvency_negative_wallet():
    """Proves negative wallet balance triggers a solvency violation."""
    with pytest.raises(SolvencyViolationError, match="Negative wallet balance"):
        assert_account_solvency(Decimal("-0.0001"), Decimal("0.0000"))


def test_account_solvency_negative_reserved():
    """Proves negative reserved cash triggers a solvency violation."""
    with pytest.raises(SolvencyViolationError, match="Negative reserved cash"):
        assert_account_solvency(Decimal("100.0000"), Decimal("-1.0000"))


def test_account_solvency_over_reservation():
    """Proves reserved cash exceeding wallet balance triggers a solvency violation."""
    with pytest.raises(SolvencyViolationError, match="Reserved cash .* exceeds wallet balance"):
        assert_account_solvency(Decimal("100.0000"), Decimal("100.0001"))


def test_holding_solvency_valid():
    """Proves valid share holding quantities pass."""
    assert_holding_solvency(100, 50)
    assert_holding_solvency(100, 100)
    assert_holding_solvency(0, 0)


def test_holding_solvency_negative_quantity():
    """Proves negative share quantity triggers a solvency violation."""
    with pytest.raises(SolvencyViolationError, match="Negative share quantity"):
        assert_holding_solvency(-1, 0)


def test_holding_solvency_over_reservation():
    """Proves reserved shares exceeding total quantity triggers a solvency violation."""
    with pytest.raises(SolvencyViolationError, match="Reserved quantity .* exceeds owned"):
        assert_holding_solvency(50, 51)


def test_balanced_journal_valid():
    """Proves balanced double-entry postings pass."""
    postings = [
        JournalPosting(
            entry_id="j_sec_1",
            fill_id="f1",
            ledger_account=JournalAccount.SECURITIES_AT_COST,
            debit=Decimal("1500.0000"),
            credit=Decimal("0.0000"),
        ),
        JournalPosting(
            entry_id="j_fee_1",
            fill_id="f1",
            ledger_account=JournalAccount.FEE_EXPENSE,
            debit=Decimal("0.7500"),
            credit=Decimal("0.0000"),
        ),
        JournalPosting(
            entry_id="j_csh_1",
            fill_id="f1",
            ledger_account=JournalAccount.CASH,
            debit=Decimal("0.0000"),
            credit=Decimal("1500.7500"),
        ),
    ]
    assert_balanced_journal(postings)


def test_unbalanced_journal_debit_credit_mismatch():
    """Proves unbalanced debits and credits trigger an UnbalancedJournalError."""
    postings = [
        JournalPosting(
            entry_id="j_sec_1",
            fill_id="f1",
            ledger_account=JournalAccount.SECURITIES_AT_COST,
            debit=Decimal("1500.0000"),
            credit=Decimal("0.0000"),
        ),
        JournalPosting(
            entry_id="j_csh_1",
            fill_id="f1",
            ledger_account=JournalAccount.CASH,
            debit=Decimal("0.0000"),
            credit=Decimal("1499.0000"),
        ),
    ]
    with pytest.raises(UnbalancedJournalError, match="UNBALANCED_JOURNAL"):
        assert_balanced_journal(postings)


def test_journal_empty_or_dual_side_rejected():
    """Proves empty postings or postings with both debit and credit are rejected."""
    with pytest.raises(UnbalancedJournalError):
        assert_balanced_journal([])

    dual_entry = [
        JournalPosting(
            entry_id="j_err_1",
            fill_id="f1",
            ledger_account=JournalAccount.CASH,
            debit=Decimal("100.0000"),
            credit=Decimal("100.0000"),
        )
    ]
    with pytest.raises(UnbalancedJournalError, match="must have either debit > 0 OR credit > 0"):
        assert_balanced_journal(dual_entry)


def test_concurrent_order_solvency_simulation():
    """
    Stress-tests concurrency logic: 10 threads attempt simultaneous orders
    when wallet balance only covers 2.
    Asserts reserved_cash never exceeds wallet_balance.
    """
    import threading

    lock = threading.Lock()
    wallet_balance = Decimal("2000.0000")
    reserved_cash = Decimal("0.0000")
    order_cost = Decimal("1000.0000")
    accepted_orders = 0
    rejected_orders = 0

    def attempt_order():
        nonlocal reserved_cash, accepted_orders, rejected_orders
        with lock:
            available = wallet_balance - reserved_cash
            if available >= order_cost:
                reserved_cash += order_cost
                assert_account_solvency(wallet_balance, reserved_cash)
                accepted_orders += 1
            else:
                rejected_orders += 1

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(attempt_order) for _ in range(10)]
        for f in futures:
            f.result()

    assert accepted_orders == 2
    assert rejected_orders == 8
    assert reserved_cash == Decimal("2000.0000")
    assert reserved_cash <= wallet_balance


@pytest.mark.skipif(
    PostgresStorage().is_sqlite_fallback,
    reason="Requires live PostgreSQL daemon on port 5432 for SELECT ... FOR UPDATE row locks",
)
def test_real_postgresql_concurrent_for_update_solvency():
    """
    Gold-Standard DBMS Concurrency Verification:
    Spawns 10 concurrent OS worker threads against PostgreSQL.
    Investor 99 starts with wallet_balance = 2000.0000 and reserved_cash = 0.0000.
    10 threads simultaneously call submit_order_intent() each requesting 1000.0000.
    Uses PostgreSQL row-level locks: SELECT ... FOR UPDATE.
    Verifies that exactly 2 orders succeed and 8 fail with SolvencyViolationError.
    Confirms PostgreSQL check constraints and solvency invariants hold in the actual database.
    """
    from datetime import datetime, timezone
    import threading
    from glassledger.domain.models import OrderIntent, OrderSide, OrderStatus
    from glassledger.storage.postgres import PostgresStorage

    pg = PostgresStorage()
    with pg.get_connection_context() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO investors (investor_id, name, email, wallet_balance, reserved_cash)
            VALUES (99, 'Concurrency Stress Investor', 'concurrency@glassledger.io', 2000.0000, 0.0000)
            ON CONFLICT (investor_id) DO UPDATE 
            SET wallet_balance = 2000.0000, reserved_cash = 0.0000;
            """
        )
        cur.execute(
            """
            INSERT INTO stocks (symbol, company_name, sector, current_price)
            VALUES ('RELIANCE', 'Reliance Industries Ltd.', 'Energy', 2500.0000)
            ON CONFLICT (symbol) DO NOTHING;
            """
        )
        cur.close()

    accepted = 0
    rejected = 0
    errors = []
    count_lock = threading.Lock()

    def run_worker(thread_idx: int):
        nonlocal accepted, rejected
        order = OrderIntent(
            order_id=f"ord_conc_{thread_idx:02d}",
            investor_id=99,
            symbol="RELIANCE",
            side=OrderSide.BUY,
            quantity=1,
            price=Decimal("1000.0000"),
            reserved_amount=Decimal("1000.0000"),
            status=OrderStatus.PENDING,
            decision_ref=f"dec_conc_{thread_idx:02d}",
            created_at=datetime.now(timezone.utc),
        )
        try:
            with pg.get_connection_context() as conn:
                pg.submit_order_intent(conn, order)
            with count_lock:
                accepted += 1
        except SolvencyViolationError:
            with count_lock:
                rejected += 1
        except Exception as e:
            with count_lock:
                errors.append(e)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(run_worker, i) for i in range(10)]
        for f in futures:
            f.result()

    assert not errors, f"Unexpected errors during concurrent test: {errors}"
    assert accepted == 2, f"Expected exactly 2 accepted orders, got {accepted}"
    assert rejected == 8, f"Expected exactly 8 rejected orders, got {rejected}"

    inv = pg.get_investor_account(99)
    assert Decimal(str(inv["wallet_balance"])) == Decimal("2000.0000")
    assert Decimal(str(inv["reserved_cash"])) == Decimal("2000.0000")

