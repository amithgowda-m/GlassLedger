"""The test session may only ever drop a database whose name ends in _test and is not the dev DB."""
import pytest

from tests._db_guard import assert_safe_to_drop, derive_test_uris


def test_accepts_dedicated_test_database():
    assert_safe_to_drop("glassledger_test", dev_db="glassledger")


@pytest.mark.parametrize("name", ["glassledger", "postgres", "template1", "prod", "glassledger_test; DROP", "test"])
def test_refuses_anything_not_named_like_a_test_database(name):
    with pytest.raises(RuntimeError, match="REFUSING"):
        assert_safe_to_drop(name, dev_db="glassledger")


def test_refuses_when_dev_database_is_itself_the_test_name():
    with pytest.raises(RuntimeError, match="REFUSING"):
        assert_safe_to_drop("glassledger_test", dev_db="glassledger_test")


def test_derive_uris_replaces_only_the_database_and_keeps_query():
    admin, test, dev = derive_test_uris(
        "postgresql://u:p@localhost:5432/glassledger?sslmode=disable", "glassledger_test"
    )
    assert dev == "glassledger"
    assert admin == "postgresql://u:p@localhost:5432/postgres?sslmode=disable"
    assert test == "postgresql://u:p@localhost:5432/glassledger_test?sslmode=disable"
