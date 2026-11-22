"""
Pytest session-scoped fixture to ensure SQLite databases are bootstrapped
(schema + seed data) before any tests run.
"""
from pathlib import Path
import pytest
from glassledger.storage.mongo import MongoStorage
from glassledger.storage.postgres import PostgresStorage


@pytest.fixture(scope="session", autouse=True)
def bootstrap_databases():
    """
    Session-scoped fixture: runs DB migrations and seeds reference data once
    before any test in the suite.
    """
    migration_file = Path("migrations/postgres/001_initial_schema.sql")
    pg = PostgresStorage()
    pg.run_migrations(migration_sql_path=str(migration_file))
    pg.seed_reference_data()

    mongo = MongoStorage()
    mongo.run_migrations()

    yield
