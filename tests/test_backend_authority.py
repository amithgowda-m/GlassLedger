"""
PostgreSQL is the only authoritative ledger backend.

- The SQLite fallback must announce itself loudly (it stores NUMERIC as binary REAL and
  has no SELECT ... FOR UPDATE row locks).
- Exact-decimal (money-precision) guarantees are asserted only against live PostgreSQL.
"""
from decimal import Decimal
import logging

import pytest

from glassledger.storage import mongo as mongo_mod
from glassledger.storage import postgres as pg_mod
from glassledger.storage.postgres import PostgresStorage


def test_sqlite_ledger_fallback_logs_non_authoritative_warning(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(pg_mod, "_FALLBACK_WARNED", False)
    pg = PostgresStorage()
    pg._probed = False
    pg._sqlite_path = tmp_path / "ledger.db"
    with caplog.at_level(logging.WARNING, logger="glassledger.storage.postgres"):
        pg.open()
    text = caplog.text
    assert "NON-AUTHORITATIVE" in text
    assert "REAL" in text and "row locks" in text


def test_sqlite_telemetry_fallback_logs_non_authoritative_warning(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(mongo_mod, "_FALLBACK_WARNED", False)
    m = mongo_mod.MongoStorage()
    m._probed = False
    m._sqlite_path = tmp_path / "telemetry.db"
    with caplog.at_level(logging.WARNING, logger="glassledger.storage.mongo"):
        m.connect()
    assert "NON-AUTHORITATIVE" in caplog.text


@pytest.mark.skipif(
    PostgresStorage().is_sqlite_fallback,
    reason="Money precision is only guaranteed (and only tested) on live PostgreSQL NUMERIC(18,4)",
)
def test_pg_money_is_exact_decimal_without_tolerance():
    """
    Amounts like 0.1 / 0.2 are not representable in binary floating point. On PostgreSQL
    they must round-trip as exact Decimals and the CASH journal must equal wallet_balance
    with plain == (no quantize, no tolerance).
    """
    from glassledger.domain.models import OrderSide
    from tests.test_settlement import new_investor, trade

    pg = PostgresStorage()
    inv = new_investor(pg, "1000.1000")
    for _ in range(3):
        trade(pg, inv, OrderSide.BUY, 1, "0.1000")
        trade(pg, inv, OrderSide.BUY, 1, "0.2000")
    trade(pg, inv, OrderSide.SELL, 4, "0.3000")

    wallet = pg.get_investor_account(inv)["wallet_balance"]
    assert isinstance(wallet, Decimal)
    row = pg._query(
        """
        SELECT SUM(j.debit) - SUM(j.credit) AS net
        FROM journal_entries j
        LEFT JOIN fills f ON f.fill_id = j.fill_id
        LEFT JOIN orders o ON o.order_id = f.order_id
        LEFT JOIN capital_events ce ON ce.event_id = j.capital_event_id
        WHERE j.ledger_account = 'CASH' AND COALESCE(o.investor_id, ce.investor_id) = %s
        """,
        (inv,),
    )[0]
    assert isinstance(row["net"], Decimal)
    assert row["net"] == wallet

    avg = pg._query(
        "SELECT avg_buy_price FROM holdings WHERE investor_id = %s AND symbol = 'INFY'", (inv,)
    )[0]["avg_buy_price"]
    assert avg == Decimal("0.1500")
