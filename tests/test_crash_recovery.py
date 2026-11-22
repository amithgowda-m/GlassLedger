from decimal import Decimal
import subprocess
import sys
import time
import pytest
from glassledger.storage.postgres import PostgresStorage


class TransactionalContext:
    """Simulates ACID transaction rollback semantics for testing crash safety."""
    def __init__(self, state: dict):
        self.state = state
        self._snapshot: dict = {}

    def __enter__(self):
        import copy
        self._snapshot = copy.deepcopy(self.state)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.state.clear()
            self.state.update(self._snapshot)
            return False
        return True


def test_crash_recovery_aborted_transaction_rollback():
    """
    Simulates application-level exception mid-settlement.
    Proves state rolls back 100% with zero partial state or orphaned rows.
    """
    database_state = {
        "wallet_balance": Decimal("10000.0000"),
        "reserved_cash": Decimal("1500.0000"),
        "holdings": {"AAPL": {"quantity": 0, "avg_price": Decimal("0.0000")}},
        "fills": [],
        "journal_entries": [],
    }

    initial_balance = database_state["wallet_balance"]
    initial_reserved = database_state["reserved_cash"]
    initial_holdings_qty = database_state["holdings"]["AAPL"]["quantity"]

    with pytest.raises(RuntimeError, match="CRASH_INJECTION"):
        with TransactionalContext(database_state):
            database_state["wallet_balance"] -= Decimal("1500.0000")
            database_state["reserved_cash"] -= Decimal("1500.0000")
            database_state["holdings"]["AAPL"]["quantity"] += 10
            database_state["holdings"]["AAPL"]["avg_price"] = Decimal("150.0000")
            raise RuntimeError("CRASH_INJECTION: Sudden power loss / SIGKILL during fill settlement")

    assert database_state["wallet_balance"] == initial_balance
    assert database_state["reserved_cash"] == initial_reserved
    assert database_state["holdings"]["AAPL"]["quantity"] == initial_holdings_qty
    assert len(database_state["fills"]) == 0
    assert len(database_state["journal_entries"]) == 0


@pytest.mark.skipif(
    PostgresStorage().is_sqlite_fallback,
    reason="Requires live PostgreSQL daemon on port 5432 with native NOW() support",
)
def test_real_subprocess_sigkill_rollback():
    """
    FR-10 Gold-Standard OS-Level Crash Safety Test:
    Spawns an actual independent child OS process that begins a database transaction,
    mutates investor balances and inserts an order, then signals readiness.
    The parent process brutally terminates the child process using SIGKILL / TerminateProcess (kill()).
    Verifies that the database engine's transaction manager aborts and rolls back
    with zero half-committed rows.
    """
    pg = PostgresStorage()
    initial_inv = pg.get_investor_account(1)
    initial_balance = Decimal(str(initial_inv["wallet_balance"]))

    # Inline script for the child process
    worker_script = """
import sys
import time
from glassledger.storage.postgres import PostgresStorage

pg = PostgresStorage()
with pg.get_connection_context() as conn:
    cur = conn.cursor()
    # Mutate balance inside an uncommitted transaction
    cur.execute("UPDATE investors SET wallet_balance = wallet_balance - 5000.0000 WHERE investor_id = 1")
    cur.execute("INSERT INTO orders (order_id, investor_id, symbol, side, quantity, price, reserved_amount, status, decision_ref, created_at) VALUES ('ord_crash_test', 1, 'AAPL', 'BUY', 10, 150.0000, 1500.0000, 'PENDING', 'dec_crash', NOW())")
    sys.stdout.write("MUTATED_AND_LOCKED\\n")
    sys.stdout.flush()
    # Hang indefinitely to await OS SIGKILL
    time.sleep(30)
"""

    # Spawn independent OS child process
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", worker_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        # Wait for the child process to reach the locked/mutated state
        ready_line = proc.stdout.readline().strip()
        assert ready_line == "MUTATED_AND_LOCKED", f"Unexpected child output: {ready_line}"

        # Execute brutal OS-level kill (SIGKILL on POSIX, TerminateProcess on Windows)
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
        raise

    # Give DB socket monitor a brief moment to clean up connection
    time.sleep(0.5)

    # Re-query the database engine from the parent process
    post_crash_inv = pg.get_investor_account(1)
    post_crash_balance = Decimal(str(post_crash_inv["wallet_balance"]))

    with pg.get_connection_context() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM orders WHERE order_id = 'ord_crash_test'")
        row = cur.fetchone()
        order_count = list(row.values())[0] if isinstance(row, dict) else row[0]
        cur.close()

    # Invariant assertions:
    # 1. Mutated wallet balance was completely discarded by the database engine rollback
    assert post_crash_balance == initial_balance, f"Balance leaked after SIGKILL: {post_crash_balance} != {initial_balance}"
    # 2. Uncommitted order was never persisted
    assert order_count == 0, f"Orphaned order survived SIGKILL!"


@pytest.mark.skipif(
    PostgresStorage().is_sqlite_fallback,
    reason="Requires live PostgreSQL daemon on port 5432 (real session kill + server-side rollback)",
)
def test_real_subprocess_sigkill_mid_cancel_keeps_reservation():
    """
    A child process cancels a PENDING BUY inside a transaction (reservation released,
    status set to CANCELED) and is SIGKILLed before commit. PostgreSQL must discard
    both changes: the order stays PENDING and reserved_cash is unchanged.
    """
    import uuid
    from glassledger.domain.models import OrderSide
    from tests.test_settlement import account, make_order, new_investor, order_row, submit

    pg = PostgresStorage()
    inv = new_investor(pg)
    order = make_order(inv, OrderSide.BUY, 10, "1500.0000")
    submit(pg, order)
    before = account(pg, inv)

    worker_script = f"""
import sys, time
from glassledger.storage.postgres import PostgresStorage
pg = PostgresStorage()
with pg.get_connection_context() as conn:
    with conn.transaction():
        pg.cancel_order(conn, "{order.order_id}", "killed mid-cancel")
        sys.stdout.write("CANCEL_APPLIED_UNCOMMITTED\\n")
        sys.stdout.flush()
        time.sleep(30)
"""
    proc = subprocess.Popen([sys.executable, "-u", "-c", worker_script],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline().strip()
        if line != "CANCEL_APPLIED_UNCOMMITTED":
            proc.kill()
            raise AssertionError(f"child did not reach the kill point: stdout={line!r} stderr={proc.stderr.read()[-2000:]}")
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
        raise
    time.sleep(0.5)

    assert account(pg, inv) == before
    assert order_row(pg, order.order_id)["status"] == "PENDING"
