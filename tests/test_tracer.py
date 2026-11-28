"""Every journal line is traceable: trade lines to their fill/order, capital lines to their capital event."""
from decimal import Decimal
import uuid

from glassledger.diagnostics.tracer import Tracer
from glassledger.domain.models import OrderSide
from glassledger.storage.mongo import MongoStorage
from glassledger.storage.postgres import PostgresStorage
from tests.test_settlement import trade


def _tracer(pg: PostgresStorage) -> Tracer:
    return Tracer(pg=pg, mongo=MongoStorage())


def test_opening_balance_line_traces_to_its_capital_event():
    pg = PostgresStorage()
    inv = pg.create_investor("Trace Test", f"t_{uuid.uuid4().hex[:8]}@test.local", Decimal("5000.0000"))
    entry = pg._query(
        "SELECT j.entry_id FROM journal_entries j JOIN capital_events c ON c.event_id = j.capital_event_id "
        "WHERE c.investor_id = %s AND j.ledger_account = 'CASH'",
        (inv,),
    )[0]["entry_id"]

    result = _tracer(pg).trace(entry)

    assert "error" not in result, result
    assert result["journal"]["account"] == "CASH"
    ce = result["capital_event"]
    assert (ce["event_type"], ce["investor_id"], Decimal(ce["amount"])) == ("OPENING_BALANCE", inv, Decimal("5000.0000"))


def test_trade_line_still_traces_to_fill_and_order():
    pg = PostgresStorage()
    inv = pg.create_investor("Trace Test", f"t_{uuid.uuid4().hex[:8]}@test.local", Decimal("100000.0000"))
    order = trade(pg, inv, OrderSide.BUY, 2, "1500.0000")

    result = _tracer(pg).trace(f"j_csh_fill_{order.order_id}")

    assert "error" not in result, result
    assert result["order"]["order_id"] == order.order_id
    assert result["fill"]["fill_id"] == f"fill_{order.order_id}"


def test_unknown_entry_reports_not_found():
    assert "error" in _tracer(PostgresStorage()).trace("j_does_not_exist")
