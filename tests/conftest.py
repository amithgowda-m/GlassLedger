"""
Test-session isolation.

Tests never touch the developer's stores:
  - SQLite fallback files go to a fresh temp directory per session.
  - Live MongoDB uses database 'glassledger_test' (dropped at session start).
  - Live PostgreSQL uses database 'glassledger_test' (dropped and recreated at session start),
    then every migration in migrations/postgres is applied.

Overrides are set both on the in-process settings object and in os.environ, so
subprocess-based tests (e.g. SIGKILL crash tests) target the same isolated stores.
"""
import os
import socket
import tempfile

import pytest

TEST_DB = "glassledger_test"


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.15)
        return s.connect_ex(("127.0.0.1", port)) == 0


from glassledger.config import settings  # noqa: E402
from tests._db_guard import assert_safe_to_drop, derive_test_uris  # noqa: E402

_tmp_dir = tempfile.mkdtemp(prefix="glassledger_test_")
_admin_uri, _test_uri, _dev_pg_db = derive_test_uris(settings.postgres_uri, TEST_DB)
_dev_mongo_db = settings.mongo_db_name
# Hard guard: fail the whole session before anything is dropped if names are unsafe.
assert_safe_to_drop(TEST_DB, dev_db=_dev_pg_db)
assert_safe_to_drop(TEST_DB, dev_db=_dev_mongo_db)
_overrides = {
    "SQLITE_DATA_DIR": _tmp_dir,
    "MONGO_DB_NAME": TEST_DB,
    "POSTGRES_URI": _test_uri,
}
os.environ.update(_overrides)
settings.sqlite_data_dir = _overrides["SQLITE_DATA_DIR"]
settings.mongo_db_name = _overrides["MONGO_DB_NAME"]
settings.postgres_uri = _overrides["POSTGRES_URI"]

PG_LIVE = _port_open(5432)
MONGO_LIVE = _port_open(27017)

if PG_LIVE:
    import psycopg

    with psycopg.connect(_admin_uri, autocommit=True) as admin:
        assert_safe_to_drop(TEST_DB, dev_db=_dev_pg_db)
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")

if MONGO_LIVE:
    from pymongo import MongoClient

    _mc = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=2000)
    assert_safe_to_drop(TEST_DB, dev_db=_dev_mongo_db)
    _mc.drop_database(TEST_DB)
    _mc.close()

from glassledger.storage.mongo import MongoStorage  # noqa: E402
from glassledger.storage.postgres import PostgresStorage  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def bootstrap_databases():
    """Runs all migrations and seeds reference data once per session, on isolated stores."""
    pg = PostgresStorage()
    pg.run_migrations()
    pg.seed_reference_data()

    mongo = MongoStorage()
    mongo.run_migrations()

    yield
