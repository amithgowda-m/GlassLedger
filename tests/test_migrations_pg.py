"""
Migration 002/003 against a legacy-shaped (pre-002) database on live PostgreSQL:
  - the duplicate decision_ref guard stops 002 without deleting anything,
  - each migration file is its own transaction (001 stays applied, 002 rolls back fully),
  - after the operator resolves the duplicate, 002/003 apply and re-running is a no-op,
  - the backfill makes the CASH journal equal wallet_balance and keeps the journal balanced.
"""
from pathlib import Path

import pytest

from glassledger.config import settings
from glassledger.storage.postgres import PostgresStorage
from tests._db_guard import assert_safe_to_drop, derive_test_uris

LEGACY_DB = "glassledger_legacy_test"

pytestmark = pytest.mark.skipif(
    PostgresStorage().is_sqlite_fallback,
    reason="Requires live PostgreSQL on port 5432 to run migrations against a legacy-shaped database",
)


@pytest.fixture
def legacy_uri():
    import psycopg

    admin_uri, legacy_uri, dev_db = derive_test_uris(settings.postgres_uri, LEGACY_DB)
    assert_safe_to_drop(LEGACY_DB, dev_db=dev_db)
    with psycopg.connect(admin_uri, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {LEGACY_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {LEGACY_DB}")
    yield legacy_uri
    with psycopg.connect(admin_uri, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {LEGACY_DB} WITH (FORCE)")


def test_002_guard_backfill_and_per_file_transactions(legacy_uri):
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(legacy_uri, row_factory=dict_row) as c:
        c.execute(Path("migrations/postgres/001_initial_schema.sql").read_text(encoding="utf-8"))
        c.execute("INSERT INTO investors (investor_id, name, email, wallet_balance) VALUES (1, 'A', 'a@x', 91400)")
        c.execute("INSERT INTO stocks VALUES ('INFY', 'Infosys', 'Tech', 1500)")
        c.execute("INSERT INTO orders VALUES ('o1', 1, 'INFY', 'BUY', 10, 1500, 0, 'FILLED', 'dec_dup', NOW())")
        c.execute("INSERT INTO orders VALUES ('o2', 1, 'INFY', 'SELL', 4, 1600, 0, 'FILLED', 'dec_dup', NOW())")
        c.execute("INSERT INTO fills VALUES ('f1', 'o1', 10, 1500, 0, NOW()), ('f2', 'o2', 4, 1600, 0, NOW())")
        # Pre-002 journals: unjournaled opening cash; old-style SELL credited at proceeds.
        c.execute(
            """INSERT INTO journal_entries (entry_id, fill_id, ledger_account, debit, credit) VALUES
               ('j1', 'f1', 'SECURITIES_AT_COST', 15000, 0), ('j2', 'f1', 'CASH', 0, 15000),
               ('j3', 'f2', 'CASH', 6400, 0), ('j4', 'f2', 'SECURITIES_AT_COST', 0, 6400)"""
        )
        c.execute("INSERT INTO holdings VALUES (1, 'INFY', 6, 0, 1500)")

    pg = PostgresStorage(legacy_uri)
    try:
        with pytest.raises(Exception, match="Cannot add uq_orders_decision_ref"):
            pg.run_migrations()

        with psycopg.connect(legacy_uri, row_factory=dict_row) as c:
            applied = [r["version"] for r in c.execute("SELECT version FROM schema_migrations ORDER BY 1")]
            assert applied == ["001_initial_schema.sql"]
            assert c.execute("SELECT to_regclass('capital_events') AS t").fetchone()["t"] is None
            assert c.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"] == 2  # nothing deleted
            c.execute("UPDATE orders SET decision_ref = 'dec_dup_2' WHERE order_id = 'o2'")

        assert pg.run_migrations() == ["002_settlement_correctness.sql", "003_journal_line_uniqueness.sql"]
        assert pg.run_migrations() == []

        with psycopg.connect(legacy_uri, row_factory=dict_row) as c:
            wallet = c.execute("SELECT wallet_balance FROM investors WHERE investor_id = 1").fetchone()["wallet_balance"]
            cash = c.execute(
                """
                SELECT SUM(j.debit) - SUM(j.credit) AS net FROM journal_entries j
                LEFT JOIN fills f ON f.fill_id = j.fill_id
                LEFT JOIN orders o ON o.order_id = f.order_id
                LEFT JOIN capital_events ce ON ce.event_id = j.capital_event_id
                WHERE j.ledger_account = 'CASH' AND COALESCE(o.investor_id, ce.investor_id) = 1
                """
            ).fetchone()["net"]
            assert cash == wallet
            events = c.execute("SELECT event_id, event_type, amount FROM capital_events").fetchall()
            assert [(e["event_id"], e["event_type"], str(e["amount"])) for e in events] == [
                ("cap_backfill_1", "OPENING_BALANCE", "100000.0000")
            ]
            assert c.execute("SELECT SUM(debit) - SUM(credit) AS d FROM journal_entries").fetchone()["d"] == 0
    finally:
        pg.close()
