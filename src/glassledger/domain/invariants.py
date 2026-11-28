from decimal import Decimal
from typing import Sequence
from glassledger.domain.models import JournalPosting


class InvariantBreachError(Exception):
    """Raised when an absolute financial invariant is violated."""
    pass


class SolvencyViolationError(InvariantBreachError):
    """Raised when account cash or holding reservations exceed solvent limits."""
    pass


class InsufficientHoldingsError(SolvencyViolationError):
    """Raised when a SELL exceeds the investor's available (unreserved) shares."""
    pass


class OrderStateError(InvariantBreachError):
    """Raised on an illegal order lifecycle transition (e.g. filling a CANCELED order)."""
    pass


class LedgerResetRefusedError(InvariantBreachError):
    """Raised when a reset/seed would orphan open orders or break ledger invariants."""
    pass


class UnbalancedJournalError(InvariantBreachError):
    """Raised when total debits do not match total credits in journal entries."""
    pass


def assert_account_solvency(wallet_balance: Decimal, reserved_cash: Decimal) -> None:
    """
    Validates investor solvency:
    1. wallet_balance >= 0
    2. reserved_cash >= 0
    3. reserved_cash <= wallet_balance
    """
    if wallet_balance < Decimal("0.0000"):
        raise SolvencyViolationError(
            f"SOLVENCY_BREACH: Negative wallet balance: {wallet_balance}"
        )
    if reserved_cash < Decimal("0.0000"):
        raise SolvencyViolationError(
            f"SOLVENCY_BREACH: Negative reserved cash: {reserved_cash}"
        )
    if reserved_cash > wallet_balance:
        raise SolvencyViolationError(
            f"SOLVENCY_BREACH: Reserved cash ({reserved_cash}) exceeds wallet balance ({wallet_balance})"
        )


def assert_holding_solvency(quantity: int, reserved_quantity: int) -> None:
    """
    Validates securities holding solvency:
    1. quantity >= 0
    2. reserved_quantity >= 0
    3. reserved_quantity <= quantity
    """
    if quantity < 0:
        raise SolvencyViolationError(
            f"SOLVENCY_BREACH: Negative share quantity: {quantity}"
        )
    if reserved_quantity < 0:
        raise SolvencyViolationError(
            f"SOLVENCY_BREACH: Negative reserved quantity: {reserved_quantity}"
        )
    if reserved_quantity > quantity:
        raise SolvencyViolationError(
            f"SOLVENCY_BREACH: Reserved quantity ({reserved_quantity}) exceeds owned quantity ({quantity})"
        )


def assert_balanced_journal(postings: Sequence[JournalPosting]) -> None:
    """
    Validates that a set of journal postings complies with double-entry accounting:
    1. Sum(Debits) == Sum(Credits)
    2. Every posting is either a debit or a credit, never both and never zero.
    """
    if not postings:
        raise UnbalancedJournalError("Journal postings set cannot be empty")

    total_debit = Decimal("0.0000")
    total_credit = Decimal("0.0000")

    for p in postings:
        if p.debit < Decimal("0.0000") or p.credit < Decimal("0.0000"):
            raise UnbalancedJournalError(
                f"Negative posting encountered: debit={p.debit}, credit={p.credit}"
            )
        if (p.debit > Decimal("0.0000") and p.credit > Decimal("0.0000")) or (
            p.debit == Decimal("0.0000") and p.credit == Decimal("0.0000")
        ):
            raise UnbalancedJournalError(
                f"Posting {p.entry_id} must have either debit > 0 OR credit > 0, got debit={p.debit}, credit={p.credit}"
            )
        total_debit += p.debit
        total_credit += p.credit

    if total_debit != total_credit:
        raise UnbalancedJournalError(
            f"UNBALANCED_JOURNAL: Total debits ({total_debit}) != Total credits ({total_credit})"
        )
