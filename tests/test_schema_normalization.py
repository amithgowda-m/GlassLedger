"""
Verifies that the schema claims made in docs/normalization.md match reality.

Three layers:
  1. Live catalog checks against a real PostgreSQL (pg_constraint / information_schema),
     after migrations 001 + 002. Skipped when PostgreSQL is not reachable on 5432.
  2. Static checks against migrations/postgres/*.sql (always run).
  3. Parity of the hand-mirrored SQLite fallback schema: same tables, FKs and unique keys
     (always run, on a throwaway SQLite file).
"""
from pathlib import Path
import re
import sqlite3

import pytest

from glassledger.storage.postgres import PostgresStorage

MIGRATIONS = sorted(Path("migrations/postgres").glob("*.sql"))

# --- What docs/normalization.md claims -------------------------------------------------

EXPECTED_TABLES = {
    "investors", "stocks", "holdings", "orders", "fills", "journal_entries",
    "ledger_accounts", "capital_events",
}

EXPECTED_PKS = {
    "investors": ["investor_id"],
    "stocks": ["symbol"],
    "holdings": ["investor_id", "symbol"],
    "orders": ["order_id"],
    "fills": ["fill_id"],
    "journal_entries": ["entry_id"],
    "ledger_accounts": ["account_code"],
    "capital_events": ["event_id"],
}

# (child_table, child_column) -> (parent_table, parent_column)
EXPECTED_FKS = {
    ("holdings", "investor_id"): ("investors", "investor_id"),
    ("holdings", "symbol"): ("stocks", "symbol"),
    ("orders", "investor_id"): ("investors", "investor_id"),
    ("orders", "symbol"): ("stocks", "symbol"),
    ("fills", "order_id"): ("orders", "order_id"),
    ("journal_entries", "fill_id"): ("fills", "fill_id"),
    ("journal_entries", "capital_event_id"): ("capital_events", "event_id"),
    ("journal_entries", "ledger_account"): ("ledger_accounts", "account_code"),
    ("capital_events", "investor_id"): ("investors", "investor_id"),
}

EXPECTED_UNIQUE = {("investors", "email"), ("orders", "decision_ref")}

# Composite UNIQUE keys (migration 003): table -> set of column tuples, in key order.
EXPECTED_COMPOSITE_UNIQUE = {
    "journal_entries": {("fill_id", "ledger_account"), ("capital_event_id", "ledger_account")},
}

EXPECTED_NAMED_CHECKS = {
    "investors": {"ck_wallet_positive", "ck_reserved_positive", "ck_cash_solvency"},
    "holdings": {"ck_holding_qty_positive", "ck_holding_reserved_positive", "ck_holding_solvency"},
    "journal_entries": {"ck_debit_or_credit", "ck_journal_single_source"},
    "ledger_accounts": {"ck_account_type"},
    "capital_events": {"ck_capital_event_type", "ck_capital_amount_nonzero", "ck_opening_positive"},
    "stocks": {"ck_stock_exchange"},
}

EXPECTED_INDEXES = {
    "idx_fills_order_id", "idx_capital_events_investor", "uq_capital_opening_per_investor",
    "uq_journal_fill_account", "uq_journal_capital_account",
}

EXPECTED_ACCOUNTS = {
    "CASH": "ASSET", "SECURITIES_AT_COST": "ASSET", "REALIZED_GAIN_LOSS": "INCOME",
    "OWNER_EQUITY": "EQUITY", "FEE_EXPENSE": "EXPENSE", "BROKERAGE_EXPENSE": "EXPENSE",
    "STT_EXPENSE": "EXPENSE", "EXCHANGE_FEE_EXPENSE": "EXPENSE", "GST_EXPENSE": "EXPENSE",
    "STAMP_DUTY_EXPENSE": "EXPENSE",
}


# --- 1. Live PostgreSQL catalog checks --------------------------------------------------

requires_pg = pytest.mark.skipif(
    PostgresStorage().is_sqlite_fallback,
    reason="Requires live PostgreSQL on port 5432 to inspect pg_constraint / information_schema",
)


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    pg = PostgresStorage()
    with pg.get_connection_context() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


@requires_pg
def test_pg_tables_and_migrations_recorded():
    tables = {
        r["table_name"]
        for r in _rows(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
    }
    assert tables == EXPECTED_TABLES | {"schema_migrations"}
    versions = {r["version"] for r in _rows("SELECT version FROM schema_migrations")}
    assert {m.name for m in MIGRATIONS} <= versions


@requires_pg
def test_pg_primary_keys():
    rows = _rows(
        """
        SELECT c.conrelid::regclass::text AS tbl, a.attname AS col, k.ord
        FROM pg_constraint c
        CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.contype = 'p' AND c.connamespace = 'public'::regnamespace
        ORDER BY tbl, k.ord
        """
    )
    actual: dict[str, list[str]] = {}
    for r in rows:
        actual.setdefault(r["tbl"], []).append(r["col"])
    for table, cols in EXPECTED_PKS.items():
        assert actual.get(table) == cols, f"PK mismatch on {table}: {actual.get(table)}"


@requires_pg
def test_pg_foreign_keys_exact():
    rows = _rows(
        """
        SELECT c.conrelid::regclass::text  AS child_tbl,
               ca.attname                  AS child_col,
               c.confrelid::regclass::text AS parent_tbl,
               pa.attname                  AS parent_col
        FROM pg_constraint c
        JOIN pg_attribute ca ON ca.attrelid = c.conrelid  AND ca.attnum = c.conkey[1]
        JOIN pg_attribute pa ON pa.attrelid = c.confrelid AND pa.attnum = c.confkey[1]
        WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace
        """
    )
    actual = {(r["child_tbl"], r["child_col"]): (r["parent_tbl"], r["parent_col"]) for r in rows}
    assert actual == EXPECTED_FKS


@requires_pg
def test_pg_unique_constraints():
    rows = _rows(
        """
        SELECT c.conrelid::regclass::text AS tbl, a.attname AS col, c.conname
        FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
        WHERE c.contype = 'u' AND c.connamespace = 'public'::regnamespace
        """
    )
    single = {}
    for r in rows:
        single.setdefault((r["tbl"], r["conname"]), []).append(r["col"])
    assert {(t, cols[0]) for (t, _), cols in single.items() if len(cols) == 1} == EXPECTED_UNIQUE
    assert "uq_orders_decision_ref" in {r["conname"] for r in rows}

    composite = _rows(
        """
        SELECT c.conname, c.conrelid::regclass::text AS tbl,
               array_agg(a.attname ORDER BY k.ord) AS cols
        FROM pg_constraint c
        CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.contype = 'u' AND c.connamespace = 'public'::regnamespace
        GROUP BY c.conname, c.conrelid HAVING COUNT(*) > 1
        """
    )
    got = {}
    for r in composite:
        got.setdefault(r["tbl"], set()).add(tuple(r["cols"]))
    assert got == EXPECTED_COMPOSITE_UNIQUE


@requires_pg
def test_pg_decision_ref_is_unique_logical_reference_without_fk():
    cols = _rows(
        "SELECT data_type, character_maximum_length, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'orders' AND column_name = 'decision_ref'"
    )
    assert cols == [{"data_type": "character varying", "character_maximum_length": 64, "is_nullable": "NO"}]
    fk_on_decision_ref = _rows(
        """
        SELECT 1 FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
        WHERE c.contype = 'f' AND c.conrelid = 'orders'::regclass AND a.attname = 'decision_ref'
        """
    )
    assert fk_on_decision_ref == []
    stray = _rows(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name IN ('strategy_id', 'execution_log_id')"
    )
    assert stray == []


@requires_pg
def test_pg_named_check_constraints():
    rows = _rows(
        """
        SELECT conrelid::regclass::text AS tbl, conname, pg_get_constraintdef(oid) AS def
        FROM pg_constraint
        WHERE contype = 'c' AND connamespace = 'public'::regnamespace
        """
    )
    by_table: dict[str, dict[str, str]] = {}
    for r in rows:
        by_table.setdefault(r["tbl"], {})[r["conname"]] = r["def"]
    for table, names in EXPECTED_NAMED_CHECKS.items():
        assert names <= set(by_table.get(table, {})), f"Missing checks on {table}"

    assert "reserved_cash <= wallet_balance" in by_table["investors"]["ck_cash_solvency"]
    assert "reserved_quantity <= quantity" in by_table["holdings"]["ck_holding_solvency"]
    d = by_table["journal_entries"]["ck_debit_or_credit"]
    assert "debit > " in d and "credit > " in d
    # The 001 inline ledger_account CHECK list was replaced by the ledger_accounts FK.
    assert not [x for x in by_table["journal_entries"].values() if "ledger_account" in x]


@requires_pg
def test_pg_ledger_accounts_content_and_nullable_fill_id():
    accounts = {r["account_code"]: r["account_type"] for r in _rows("SELECT * FROM ledger_accounts")}
    assert accounts == EXPECTED_ACCOUNTS
    fill_col = _rows(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'journal_entries' AND column_name = 'fill_id'"
    )
    assert fill_col == [{"is_nullable": "YES"}]


@requires_pg
def test_pg_indexes():
    idx = {r["indexname"] for r in _rows("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")}
    assert EXPECTED_INDEXES <= idx
    assert "idx_orders_decision_ref" not in idx  # superseded by uq_orders_decision_ref
    assert not {"idx_journal_fill_id", "idx_journal_capital_event_id"} & idx  # superseded in 003


@requires_pg
def test_pg_money_columns_are_numeric_not_float():
    rows = _rows(
        """
        SELECT table_name, column_name, data_type FROM information_schema.columns
        WHERE table_schema = 'public' AND data_type IN ('real', 'double precision')
        """
    )
    assert rows == []


# --- 2. Static checks on the migration files (always run) -------------------------------

@pytest.fixture(scope="module")
def migration_sql() -> str:
    return "\n".join(m.read_text(encoding="utf-8") for m in MIGRATIONS)


def test_migration_files_are_001_to_004():
    assert [m.name for m in MIGRATIONS] == [
        "001_initial_schema.sql", "002_settlement_correctness.sql", "003_journal_line_uniqueness.sql",
        "004_stock_exchange.sql",
    ]


def test_migrations_declare_exactly_the_documented_tables(migration_sql: str):
    assert set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", migration_sql)) == EXPECTED_TABLES


def test_migrations_declare_named_constraints_and_indexes(migration_sql: str):
    for names in EXPECTED_NAMED_CHECKS.values():
        for name in names:
            assert re.search(rf"CONSTRAINT {name}\s+CHECK", migration_sql), name
    for name in EXPECTED_INDEXES:
        assert name in migration_sql
    assert "ADD CONSTRAINT uq_orders_decision_ref UNIQUE (decision_ref)" in migration_sql
    assert "DROP INDEX IF EXISTS idx_orders_decision_ref" in migration_sql
    assert "UNIQUE (fill_id, ledger_account)" in migration_sql
    assert "UNIQUE (capital_event_id, ledger_account)" in migration_sql


def test_migrations_seed_the_documented_chart_of_accounts(migration_sql: str):
    for code, acct_type in EXPECTED_ACCOUNTS.items():
        assert f"('{code}', '{acct_type}')" in migration_sql, code


def test_migration_decision_ref_has_no_references_clause(migration_sql: str):
    line = next(l for l in migration_sql.splitlines() if l.strip().startswith("decision_ref"))
    assert "REFERENCES" not in line


def test_migrations_have_no_float_money(migration_sql: str):
    code = re.sub(r"--[^\n]*", "", migration_sql)  # ignore SQL comments
    assert not re.search(r"\b(REAL|FLOAT|DOUBLE PRECISION)\b", code, re.IGNORECASE)


# --- 3. SQLite fallback mirrors the PostgreSQL schema ------------------------------------

@pytest.fixture(scope="module")
def sqlite_fallback(tmp_path_factory) -> sqlite3.Connection:
    pg = PostgresStorage()
    pg._probed = False  # force fallback regardless of whether PostgreSQL is up
    pg._sqlite_path = tmp_path_factory.mktemp("parity") / "ledger.db"
    pg._ensure_sqlite_schema()
    conn = sqlite3.connect(str(pg._sqlite_path))
    yield conn
    conn.close()


def test_sqlite_fallback_has_same_tables(sqlite_fallback):
    tables = {r[0] for r in sqlite_fallback.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )}
    assert tables == EXPECTED_TABLES


def test_sqlite_fallback_has_same_foreign_keys(sqlite_fallback):
    fks = {}
    for table in EXPECTED_TABLES:
        for row in sqlite_fallback.execute(f"PRAGMA foreign_key_list({table})"):
            # row: id, seq, parent_table, from_col, to_col, ...
            fks[(table, row[3])] = (row[2], row[4])
    assert fks == EXPECTED_FKS


def test_sqlite_fallback_has_same_unique_keys(sqlite_fallback):
    uniques, composite = set(), {}
    for table in EXPECTED_TABLES:
        for idx in sqlite_fallback.execute(f"PRAGMA index_list({table})"):
            name, is_unique, origin, is_partial = idx[1], idx[2], idx[3], idx[4]
            if is_unique and origin == "u" and not is_partial:
                cols = [c[2] for c in sqlite_fallback.execute(f"PRAGMA index_info({name})")]
                if len(cols) == 1:
                    uniques.add((table, cols[0]))
                else:
                    composite.setdefault(table, set()).add(tuple(cols))
    assert uniques == EXPECTED_UNIQUE
    assert composite == EXPECTED_COMPOSITE_UNIQUE


def test_sqlite_fallback_has_same_chart_of_accounts(sqlite_fallback):
    assert dict(sqlite_fallback.execute("SELECT account_code, account_type FROM ledger_accounts")) == EXPECTED_ACCOUNTS


@requires_pg
def test_pg_stock_exchange_column_is_nullable_and_constrained():
    col = _rows(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'stocks' AND column_name = 'exchange'"
    )
    assert col == [{"data_type": "character varying", "is_nullable": "YES"}]


def test_sqlite_fallback_stocks_has_exchange_column(sqlite_fallback):
    cols = [r[1] for r in sqlite_fallback.execute("PRAGMA table_info(stocks)")]
    assert "exchange" in cols
