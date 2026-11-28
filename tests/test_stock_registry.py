"""Registering market-data symbols adds new stocks rows and never modifies existing ones."""
from decimal import Decimal
import uuid

import pytest

from glassledger.storage.postgres import PostgresStorage


def _row(pg: PostgresStorage, symbol: str) -> dict:
    return pg._query("SELECT symbol, company_name, sector, current_price, exchange FROM stocks WHERE symbol = %s", (symbol,))[0]


def test_register_new_symbol_records_company_and_exchange():
    pg = PostgresStorage()
    sym = f"T{uuid.uuid4().hex[:7].upper()}"
    assert pg.register_stock(sym, "Test Industries Ltd.", exchange="NSE", sector="Equities",
                             current_price=Decimal("1234.5000")) is True
    row = _row(pg, sym)
    assert (row["company_name"], row["exchange"], row["sector"]) == ("Test Industries Ltd.", "NSE", "Equities")
    assert Decimal(str(row["current_price"])).quantize(Decimal("0.0001")) == Decimal("1234.5000")


def test_register_existing_symbol_leaves_row_untouched():
    pg = PostgresStorage()
    before = _row(pg, "RELIANCE")   # seeded reference row
    assert pg.register_stock("RELIANCE", "Something Else", exchange="NSE", sector="Other",
                             current_price=Decimal("1.0000")) is False
    assert _row(pg, "RELIANCE") == before


def test_exchange_is_constrained():
    import sqlite3
    import psycopg

    pg = PostgresStorage()
    with pytest.raises((ValueError, sqlite3.IntegrityError, psycopg.IntegrityError)):
        pg.register_stock(f"T{uuid.uuid4().hex[:7].upper()}", "Bad Exchange Ltd.", exchange="MOON",
                          sector="Equities", current_price=Decimal("10.0000"))
