"""Hard safety guard for the databases the test session drops and recreates."""
import re
from urllib.parse import urlsplit, urlunsplit

_TEST_DB_NAME = re.compile(r"[a-z][a-z0-9_]*_test")


def assert_safe_to_drop(name: str, dev_db: str) -> None:
    """Raise unless `name` is a dedicated test database distinct from the developer's database."""
    if not _TEST_DB_NAME.fullmatch(name):
        raise RuntimeError(f"REFUSING to drop database {name!r}: test databases must match {_TEST_DB_NAME.pattern}")
    if name == dev_db:
        raise RuntimeError(f"REFUSING to drop database {name!r}: it is the configured development database")


def derive_test_uris(dev_uri: str, test_db: str) -> tuple[str, str, str]:
    """Return (admin_uri, test_uri, dev_db_name), changing only the database path of the URI."""
    parts = urlsplit(dev_uri)
    dev_db = parts.path.lstrip("/")

    def with_db(db: str) -> str:
        return urlunsplit((parts.scheme, parts.netloc, "/" + db, parts.query, parts.fragment))

    return with_db("postgres"), with_db(test_db), dev_db
