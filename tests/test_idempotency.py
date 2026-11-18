from decimal import Decimal
from glassledger.domain.invariants import assert_account_solvency, assert_balanced_journal
from glassledger.domain.models import JournalAccount, JournalPosting


class MockLedgerState:
    """Simulates PostgreSQL row locks and transaction settlement in memory."""
    def __init__(self, initial_cash: Decimal = Decimal("10000.0000")):
        self.wallet_balance = initial_cash
        self.reserved_cash = Decimal("0.0000")
        self.holdings: dict[str, dict] = {}
        self.fills: set[str] = set()
        self.journal_entries: list[JournalPosting] = []

    def submit_order_intent(self, order_id: str, symbol: str, reserved_amount: Decimal) -> str:
        available = self.wallet_balance - self.reserved_cash
        if available < reserved_amount:
            raise ValueError("INSUFFICIENT_FUNDS")
        self.reserved_cash += reserved_amount
        return order_id

    def process_fill(self, fill_data: dict) -> str:
        fill_id = fill_data["fill_id"]
        # FR-6: Idempotency check before mutation
        if fill_id in self.fills:
            return "ALREADY_APPLIED"

        qty = int(fill_data["quantity"])
        price = Decimal(str(fill_data["price"]))
        fee = Decimal(str(fill_data.get("fee", "0.0000")))
        cost_of_shares = qty * price
        total_cost = cost_of_shares + fee
        reserved_amount = Decimal(str(fill_data.get("reserved_amount", total_cost)))

        # Settle
        self.reserved_cash -= reserved_amount
        self.wallet_balance -= total_cost
        assert_account_solvency(self.wallet_balance, self.reserved_cash)

        # Upsert holding
        symbol = fill_data["symbol"]
        if symbol in self.holdings:
            curr = self.holdings[symbol]
            new_qty = curr["quantity"] + qty
            new_avg = ((curr["quantity"] * curr["avg_price"]) + cost_of_shares) / Decimal(new_qty)
            self.holdings[symbol] = {"quantity": new_qty, "avg_price": new_avg}
        else:
            self.holdings[symbol] = {"quantity": qty, "avg_price": price}

        # Add double-entry journal postings
        postings = [
            JournalPosting(
                entry_id=f"j_sec_{fill_id}",
                fill_id=fill_id,
                ledger_account=JournalAccount.SECURITIES_AT_COST,
                debit=cost_of_shares,
                credit=Decimal("0.0000"),
            ),
            JournalPosting(
                entry_id=f"j_fee_{fill_id}",
                fill_id=fill_id,
                ledger_account=JournalAccount.FEE_EXPENSE,
                debit=fee,
                credit=Decimal("0.0000"),
            ),
            JournalPosting(
                entry_id=f"j_csh_{fill_id}",
                fill_id=fill_id,
                ledger_account=JournalAccount.CASH,
                debit=Decimal("0.0000"),
                credit=total_cost,
            ),
        ]
        assert_balanced_journal(postings)
        self.journal_entries.extend(postings)
        self.fills.add(fill_id)
        return "APPLIED"


def test_duplicate_fill_has_no_second_effect():
    """
    FR-6 Verification:
    Proves executing the same fill twice returns 'ALREADY_APPLIED'
    and balances, holdings, and journal rows remain 100% unchanged on the second call.
    """
    ledger = MockLedgerState(initial_cash=Decimal("10000.0000"))
    fill_payload = {
        "fill_id": "fill_test_001",
        "order_id": "ord_test_001",
        "symbol": "AAPL",
        "quantity": 10,
        "price": "150.0000",
        "fee": "0.7500",
        "reserved_amount": "1500.7500",
    }

    # Step 1: Submit reservation
    ledger.submit_order_intent("ord_test_001", "AAPL", Decimal("1500.7500"))
    assert ledger.reserved_cash == Decimal("1500.7500")

    # Step 2: First Execution
    result_1 = ledger.process_fill(fill_payload)
    assert result_1 == "APPLIED"
    assert ledger.wallet_balance == Decimal("8499.2500")
    assert ledger.reserved_cash == Decimal("0.0000")
    assert ledger.holdings["AAPL"]["quantity"] == 10
    assert len(ledger.journal_entries) == 3

    # Capture state snapshot
    snapshot_balance = ledger.wallet_balance
    snapshot_reserved = ledger.reserved_cash
    snapshot_qty = ledger.holdings["AAPL"]["quantity"]
    snapshot_journal_len = len(ledger.journal_entries)

    # Step 3: Duplicate Execution with EXACT same fill_id
    result_2 = ledger.process_fill(fill_payload)
    assert result_2 == "ALREADY_APPLIED"

    # Step 4: Verify zero secondary financial mutations
    assert ledger.wallet_balance == snapshot_balance
    assert ledger.reserved_cash == snapshot_reserved
    assert ledger.holdings["AAPL"]["quantity"] == snapshot_qty
    assert len(ledger.journal_entries) == snapshot_journal_len
