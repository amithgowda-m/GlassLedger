from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import logging
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Any, Iterator, Optional

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from glassledger.config import settings
from glassledger.domain.invariants import (
    InsufficientHoldingsError,
    LedgerResetRefusedError,
    OrderStateError,
    SolvencyViolationError,
    assert_account_solvency,
    assert_holding_solvency,
    assert_balanced_journal,
)
from glassledger.domain.models import JournalPosting, JournalAccount, OrderIntent, OrderSide, OrderStatus

log = logging.getLogger("glassledger.storage.postgres")

# sqlite3 has no native Decimal/datetime binding; bind them as text.
# (SQLite NUMERIC affinity still converts numeric text to REAL on storage.)
sqlite3.register_adapter(Decimal, str)
sqlite3.register_adapter(datetime, lambda d: d.isoformat())

MIGRATIONS_DIR = Path("migrations/postgres")
MONEY_Q = Decimal("0.0001")

# Bump when the hand-mirrored SQLite DDL below changes incompatibly.
SQLITE_SCHEMA_VERSION = 2

SQLITE_DDL = [
    """
    CREATE TABLE IF NOT EXISTS investors (
        investor_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        wallet_balance NUMERIC(18, 4) NOT NULL DEFAULT 0.0000,
        reserved_cash NUMERIC(18, 4) NOT NULL DEFAULT 0.0000,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK (wallet_balance >= 0),
        CHECK (reserved_cash >= 0),
        CHECK (reserved_cash <= wallet_balance)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stocks (
        symbol TEXT PRIMARY KEY,
        company_name TEXT NOT NULL,
        sector TEXT NOT NULL,
        current_price NUMERIC(18, 4) NOT NULL CHECK (current_price > 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS holdings (
        investor_id INTEGER REFERENCES investors(investor_id) ON DELETE CASCADE,
        symbol TEXT REFERENCES stocks(symbol) ON DELETE RESTRICT,
        quantity INTEGER NOT NULL DEFAULT 0,
        reserved_quantity INTEGER NOT NULL DEFAULT 0,
        avg_buy_price NUMERIC(18, 4) NOT NULL DEFAULT 0.0000,
        PRIMARY KEY (investor_id, symbol),
        CHECK (quantity >= 0),
        CHECK (reserved_quantity >= 0),
        CHECK (reserved_quantity <= quantity)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        investor_id INTEGER NOT NULL REFERENCES investors(investor_id),
        symbol TEXT NOT NULL REFERENCES stocks(symbol),
        side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
        quantity INTEGER NOT NULL CHECK (quantity > 0),
        price NUMERIC(18, 4) NOT NULL CHECK (price > 0),
        reserved_amount NUMERIC(18, 4) NOT NULL DEFAULT 0.0000,
        status TEXT NOT NULL CHECK (status IN ('PENDING', 'FILLED', 'REJECTED', 'CANCELED')),
        decision_ref TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        status_reason TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fills (
        fill_id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL REFERENCES orders(order_id),
        quantity INTEGER NOT NULL CHECK (quantity > 0),
        price NUMERIC(18, 4) NOT NULL CHECK (price > 0),
        fee NUMERIC(18, 4) NOT NULL DEFAULT 0.0000 CHECK (fee >= 0),
        executed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger_accounts (
        account_code TEXT PRIMARY KEY,
        account_type TEXT NOT NULL CHECK (account_type IN ('ASSET', 'LIABILITY', 'EQUITY', 'INCOME', 'EXPENSE'))
    )
    """,
    """
    INSERT INTO ledger_accounts (account_code, account_type) VALUES
        ('CASH', 'ASSET'),
        ('SECURITIES_AT_COST', 'ASSET'),
        ('REALIZED_GAIN_LOSS', 'INCOME'),
        ('FEE_EXPENSE', 'EXPENSE'),
        ('BROKERAGE_EXPENSE', 'EXPENSE'),
        ('STT_EXPENSE', 'EXPENSE'),
        ('EXCHANGE_FEE_EXPENSE', 'EXPENSE'),
        ('GST_EXPENSE', 'EXPENSE'),
        ('STAMP_DUTY_EXPENSE', 'EXPENSE'),
        ('OWNER_EQUITY', 'EQUITY')
    ON CONFLICT (account_code) DO NOTHING
    """,
    """
    CREATE TABLE IF NOT EXISTS capital_events (
        event_id TEXT PRIMARY KEY,
        investor_id INTEGER NOT NULL REFERENCES investors(investor_id),
        event_type TEXT NOT NULL CHECK (event_type IN ('OPENING_BALANCE', 'CAPITAL_ADJUSTMENT')),
        amount NUMERIC(18, 4) NOT NULL CHECK (amount <> 0),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK (event_type <> 'OPENING_BALANCE' OR amount > 0)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_capital_opening_per_investor
        ON capital_events (investor_id) WHERE event_type = 'OPENING_BALANCE'
    """,
    """
    CREATE TABLE IF NOT EXISTS journal_entries (
        entry_id TEXT PRIMARY KEY,
        fill_id TEXT REFERENCES fills(fill_id),
        capital_event_id TEXT REFERENCES capital_events(event_id),
        ledger_account TEXT NOT NULL REFERENCES ledger_accounts(account_code),
        debit NUMERIC(18, 4) NOT NULL DEFAULT 0.0000 CHECK (debit >= 0),
        credit NUMERIC(18, 4) NOT NULL DEFAULT 0.0000 CHECK (credit >= 0),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK ((debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0)),
        CHECK ((fill_id IS NULL) <> (capital_event_id IS NULL))
    )
    """,
]


class _Cursor:
    """Thin dialect shim: write SQL once with %s placeholders; SQLite gets ? and no FOR UPDATE."""

    def __init__(self, conn: Any):
        self.conn = conn
        self.sqlite = isinstance(conn, sqlite3.Connection)
        self.cur = conn.cursor()

    def run(self, sql: str, params: tuple = ()) -> "_Cursor":
        if self.sqlite:
            sql = sql.replace("%s", "?").replace("FOR UPDATE", "")
        self.cur.execute(sql, params)
        return self

    def one(self) -> Any:
        return self.cur.fetchone()

    def all(self) -> list[Any]:
        return self.cur.fetchall()

    def close(self) -> None:
        self.cur.close()

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


_FALLBACK_WARNED = False


def _warn_fallback_once(path: Path) -> None:
    global _FALLBACK_WARNED
    if _FALLBACK_WARNED:
        return
    _FALLBACK_WARNED = True
    log.warning(
        "\n" + "=" * 78 + "\n"
        "  GLASSLEDGER LEDGER IS RUNNING ON THE NON-AUTHORITATIVE SQLITE FALLBACK (%s)\n"
        "  PostgreSQL is unreachable on 127.0.0.1:5432. In this mode:\n"
        "    - money columns are stored as binary floating point (SQLite NUMERIC -> REAL);\n"
        "    - there are no SELECT ... FOR UPDATE row locks, so concurrent solvency\n"
        "      guarantees do not hold.\n"
        "  Use it for demos only. Start PostgreSQL (docker compose up -d postgres) for real use.\n"
        + "=" * 78,
        path,
    )


def _dec(v: Any) -> Decimal:
    return Decimal(str(v if v is not None else "0"))


class PostgresStorage:
    """
    Financial Ledger Storage.
    Authoritative backend: PostgreSQL 16+ via psycopg3 connection pooling.
    If PostgreSQL is unreachable, falls back to a NON-AUTHORITATIVE SQLite store
    (no row locks, NUMERIC stored as binary REAL) intended for demos only.
    """

    def __init__(self, connection_uri: Optional[str] = None):
        self.uri = connection_uri or settings.postgres_uri
        self._pool: Optional[ConnectionPool] = None
        self._probed: Optional[bool] = None
        self._sqlite_path = Path(settings.sqlite_data_dir) / "glassledger_ledger.db"
        self._sqlite_ready = False

    @property
    def is_sqlite_fallback(self) -> bool:
        return not self._probe_postgres()

    def _probe_postgres(self) -> bool:
        """Instantaneous TCP socket check for PostgreSQL port 5432."""
        if self._probed is not None:
            return self._probed
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.15)
                res = s.connect_ex(("127.0.0.1", 5432))
                self._probed = (res == 0)
        except Exception:
            self._probed = False
        return self._probed

    def open(self) -> None:
        if self._probe_postgres():
            if self._pool is None:
                self._pool = ConnectionPool(
                    conninfo=self.uri,
                    min_size=settings.postgres_pool_min,
                    max_size=settings.postgres_pool_max,
                    kwargs={"row_factory": dict_row},
                )
        else:
            _warn_fallback_once(self._sqlite_path)
            self._ensure_sqlite_schema()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    @property
    def pool(self) -> Optional[ConnectionPool]:
        if not self.is_sqlite_fallback and self._pool is None:
            self.open()
        return self._pool

    # ------------------------------------------------------------------ SQLite fallback

    def _raw_sqlite_conn(self) -> sqlite3.Connection:
        self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._sqlite_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def _ensure_sqlite_schema(self) -> None:
        """
        Creates the mirrored schema. A fallback DB created by an older schema version is
        archived (renamed, never deleted) and a fresh one is created: the fallback is
        non-authoritative, and rebuilding SQLite constraints in place is error-prone.
        """
        if self._sqlite_ready:
            return
        conn = self._raw_sqlite_conn()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'investors'"
        ).fetchone()[0]
        if has_tables and version < SQLITE_SCHEMA_VERSION:
            conn.close()
            stamp = time.strftime("%Y%m%d%H%M%S")
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(self._sqlite_path) + suffix)
                if src.exists():
                    src.rename(src.with_name(f"{self._sqlite_path.stem}.legacy-v{version}-{stamp}.db{suffix}"))
            log.warning(
                "SQLite fallback ledger at %s used schema v%s; archived it and created a fresh v%s store.",
                self._sqlite_path, version, SQLITE_SCHEMA_VERSION,
            )
            conn = self._raw_sqlite_conn()
        with conn:
            for ddl in SQLITE_DDL:
                conn.execute(ddl)
            conn.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")
        conn.close()
        self._sqlite_ready = True

    def get_sqlite_conn(self) -> sqlite3.Connection:
        self._ensure_sqlite_schema()
        return self._raw_sqlite_conn()

    # ------------------------------------------------------------------ connections

    def get_connection_context(self):
        """Context manager yielding a connection; commits on success, rolls back on error."""
        self.open()
        if not self.is_sqlite_fallback:
            assert self.pool is not None
            return self.pool.connection()

        storage = self

        class SQLiteContext:
            def __enter__(self_ctx):
                self_ctx.conn = storage.get_sqlite_conn()
                return self_ctx.conn

            def __exit__(self_ctx, exc_type, exc_val, exc_tb):
                if exc_type is None:
                    self_ctx.conn.commit()
                else:
                    self_ctx.conn.rollback()
                self_ctx.conn.close()

        return SQLiteContext()

    @staticmethod
    @contextmanager
    def transaction(conn: Any) -> Iterator[Any]:
        """One atomic unit on an open connection, for either backend."""
        if isinstance(conn, sqlite3.Connection):
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        else:
            with conn.transaction():
                yield conn

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self.get_connection_context() as conn:
            with _Cursor(conn) as c:
                return [dict(r) for r in c.run(sql, params).all()]

    # ------------------------------------------------------------------ migrations & seed

    def run_migrations(self, migrations_path: Optional[str] = None) -> list[str]:
        """
        PostgreSQL: applies every migrations/postgres/*.sql not yet recorded in
        schema_migrations, in filename order, each in its own transaction.
        SQLite fallback: creates the hand-mirrored schema.
        Accepts a directory or (for backward compatibility) a file inside it.
        """
        path = Path(migrations_path) if migrations_path else MIGRATIONS_DIR
        if path.is_file():
            path = path.parent
        self.open()
        if self.is_sqlite_fallback:
            self._ensure_sqlite_schema()
            return [f"sqlite-fallback-v{SQLITE_SCHEMA_VERSION}"]

        assert self.pool is not None
        applied: list[str] = []
        with self.pool.connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version VARCHAR(128) PRIMARY KEY,"
                " applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            conn.commit()
            done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
            for f in sorted(path.glob("*.sql")):
                if f.name in done:
                    continue
                with conn.transaction():
                    conn.execute(f.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (f.name,))
                applied.append(f.name)
        return applied

    SEED_INVESTOR_ID = 1
    SEED_OPENING_BALANCE = Decimal("100000.0000")

    def seed_reference_data(self) -> None:
        """
        Ensures investor 1 exists with 100000.0000 of journaled cash, and seeds stocks.
        An existing investor is reset only via a journaled CAPITAL_ADJUSTMENT, and the
        reset is refused while that investor has non-terminal (PENDING) orders.
        """
        if self.get_investor_account(self.SEED_INVESTOR_ID):
            self.reset_investor_cash(self.SEED_INVESTOR_ID, self.SEED_OPENING_BALANCE)
        else:
            self.create_investor(
                name="Quantitative Fund Alpha", email="alpha@glassledger.io",
                opening_balance=self.SEED_OPENING_BALANCE, investor_id=self.SEED_INVESTOR_ID,
            )
        with self.get_connection_context() as conn:
            with _Cursor(conn) as c:
                self._seed_stocks(c)

    def create_investor(
        self, name: str, email: str, opening_balance: Decimal, investor_id: Optional[int] = None
    ) -> int:
        """Creates an investor and journals the starting capital (CASH Dr / OWNER_EQUITY Cr) atomically."""
        with self.get_connection_context() as conn:
            with self.transaction(conn), _Cursor(conn) as c:
                if investor_id is None:
                    row = c.run(
                        "INSERT INTO investors (name, email, wallet_balance, reserved_cash) "
                        "VALUES (%s, %s, 0, 0) RETURNING investor_id",
                        (name, email),
                    ).one()
                    investor_id = int(row["investor_id"])
                else:
                    c.run(
                        "INSERT INTO investors (investor_id, name, email, wallet_balance, reserved_cash) "
                        "VALUES (%s, %s, %s, 0, 0)",
                        (investor_id, name, email),
                    )
                    if not c.sqlite:  # keep SERIAL ahead of explicitly supplied ids
                        c.run(
                            "SELECT setval(pg_get_serial_sequence('investors', 'investor_id'), "
                            "(SELECT MAX(investor_id) FROM investors))"
                        )
                if opening_balance > 0:
                    self._apply_capital_event(c, investor_id, "OPENING_BALANCE", opening_balance)
        return investor_id

    def reset_investor_cash(self, investor_id: int, target_balance: Decimal) -> None:
        """
        Brings wallet_balance to target_balance through a journaled CAPITAL_ADJUSTMENT.
        Refused if the investor has non-terminal orders (their reservations would be orphaned).
        """
        with self.get_connection_context() as conn:
            with self.transaction(conn), _Cursor(conn) as c:
                inv = c.run(
                    "SELECT wallet_balance, reserved_cash FROM investors WHERE investor_id = %s FOR UPDATE",
                    (investor_id,),
                ).one()
                if not inv:
                    raise ValueError(f"Investor {investor_id} not found")
                open_orders = int(c.run(
                    "SELECT COUNT(*) AS n FROM orders WHERE investor_id = %s AND status = 'PENDING'",
                    (investor_id,),
                ).one()["n"])
                if open_orders > 0:
                    raise LedgerResetRefusedError(
                        f"RESET_REFUSED: investor {investor_id} has {open_orders} non-terminal (PENDING) order(s); "
                        "cancel or settle them first"
                    )
                if _dec(inv["reserved_cash"]) != 0:
                    raise LedgerResetRefusedError(
                        f"RESET_REFUSED: investor {investor_id} has reserved_cash {inv['reserved_cash']} "
                        "but no PENDING orders; ledger invariant already broken"
                    )
                gap = (target_balance - _dec(inv["wallet_balance"])).quantize(MONEY_Q)
                if gap != 0:
                    self._apply_capital_event(c, investor_id, "CAPITAL_ADJUSTMENT", gap)

    def _apply_capital_event(self, c: _Cursor, investor_id: int, event_type: str, amount: Decimal) -> str:
        """Posts a capital event, its two journal lines and the wallet change, inside the caller's transaction."""
        event_id = f"cap_{uuid.uuid4().hex}"
        inv = c.run(
            "SELECT wallet_balance, reserved_cash FROM investors WHERE investor_id = %s FOR UPDATE", (investor_id,)
        ).one()
        new_balance = _dec(inv["wallet_balance"]) + amount
        assert_account_solvency(new_balance, _dec(inv["reserved_cash"]))
        c.run(
            "INSERT INTO capital_events (event_id, investor_id, event_type, amount) VALUES (%s, %s, %s, %s)",
            (event_id, investor_id, event_type, amount),
        )
        cash_dr, cash_cr = (amount, Decimal("0")) if amount > 0 else (Decimal("0"), -amount)
        self._insert_postings(c, [
            JournalPosting(entry_id=f"j_csh_{event_id}", capital_event_id=event_id,
                           ledger_account=JournalAccount.CASH, debit=cash_dr, credit=cash_cr),
            JournalPosting(entry_id=f"j_eqt_{event_id}", capital_event_id=event_id,
                           ledger_account=JournalAccount.OWNER_EQUITY, debit=cash_cr, credit=cash_dr),
        ])
        c.run(
            "UPDATE investors SET wallet_balance = %s, updated_at = CURRENT_TIMESTAMP WHERE investor_id = %s",
            (new_balance, investor_id),
        )
        return event_id

    @staticmethod
    def _seed_stocks(c: _Cursor) -> None:
        c.run(
            """
            INSERT INTO stocks (symbol, company_name, sector, current_price)
            VALUES
                ('RELIANCE', 'Reliance Industries Ltd.', 'Energy', 2850.0000),
                ('TCS', 'Tata Consultancy Services', 'Technology', 3920.0000),
                ('INFY', 'Infosys Ltd.', 'Technology', 1840.0000),
                ('HDFCBANK', 'HDFC Bank Ltd.', 'Banking', 1650.0000),
                ('AAPL', 'Apple Inc.', 'Technology', 19500.0000)
            ON CONFLICT (symbol) DO UPDATE SET current_price = EXCLUDED.current_price
            """
        )

    # ------------------------------------------------------------------ order intake

    def submit_order_intent(self, conn: Any, order: OrderIntent) -> str:
        """
        Submits an order intent with row-level lock on the investor.
        Locks strictly in canonical order (investor first).
        """
        with _Cursor(conn) as c:
            inv = c.run(
                "SELECT wallet_balance, reserved_cash FROM investors WHERE investor_id = %s FOR UPDATE",
                (order.investor_id,),
            ).one()
            if not inv:
                raise ValueError(f"Investor {order.investor_id} not found")

            # Idempotent intake: an order id that already exists (in any state) is never
            # re-reserved or resurrected. Checked under the investor lock.
            if c.run("SELECT 1 FROM orders WHERE order_id = %s", (order.order_id,)).one():
                return order.order_id

            wallet_balance = _dec(inv["wallet_balance"])
            reserved_cash = _dec(inv["reserved_cash"])
            available_cash = wallet_balance - reserved_cash

            if order.side == OrderSide.BUY:
                if available_cash < order.reserved_amount:
                    raise SolvencyViolationError(
                        f"INSUFFICIENT_FUNDS: Available cash {available_cash} < Required {order.reserved_amount}"
                    )
                new_reserved = reserved_cash + order.reserved_amount
                assert_account_solvency(wallet_balance, new_reserved)
                c.run(
                    "UPDATE investors SET reserved_cash = %s, updated_at = CURRENT_TIMESTAMP WHERE investor_id = %s",
                    (new_reserved, order.investor_id),
                )
            else:
                # Canonical lock order: investor (above) -> holding.
                h = c.run(
                    "SELECT quantity, reserved_quantity FROM holdings "
                    "WHERE investor_id = %s AND symbol = %s FOR UPDATE",
                    (order.investor_id, order.symbol),
                ).one()
                available = (int(h["quantity"]) - int(h["reserved_quantity"])) if h else 0
                if available < order.quantity:
                    raise InsufficientHoldingsError(
                        f"INSUFFICIENT_HOLDINGS: investor {order.investor_id} has {available} "
                        f"unreserved {order.symbol} shares; SELL needs {order.quantity}"
                    )
                new_reserved_qty = int(h["reserved_quantity"]) + order.quantity
                assert_holding_solvency(int(h["quantity"]), new_reserved_qty)
                c.run(
                    "UPDATE holdings SET reserved_quantity = %s WHERE investor_id = %s AND symbol = %s",
                    (new_reserved_qty, order.investor_id, order.symbol),
                )

            c.run(
                """
                INSERT INTO stocks (symbol, company_name, sector, current_price)
                VALUES (%s, %s, 'Equities', %s)
                ON CONFLICT (symbol) DO UPDATE SET current_price = EXCLUDED.current_price
                """,
                (order.symbol, order.symbol, order.price),
            )
            c.run(
                """
                INSERT INTO orders (order_id, investor_id, symbol, side, quantity, price, reserved_amount, status, decision_ref, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    order.order_id, order.investor_id, order.symbol, str(order.side), order.quantity,
                    order.price, order.reserved_amount, str(order.status), order.decision_ref, order.created_at,
                ),
            )
        return order.order_id

    # ------------------------------------------------------------------ settlement

    def _generate_fee_postings(
        self, fill_id: str, fee: Decimal, breakdown: Optional[dict[str, Any]]
    ) -> list[JournalPosting]:
        """
        FR-13: Generates individual double-entry journal postings for each charge category.
        Brokerage, STT, Exchange Turnover charges, GST, and Stamp Duty.
        """
        postings: list[JournalPosting] = []
        if breakdown and isinstance(breakdown, dict):
            mapping = [
                ("brokerage", "brk", JournalAccount.BROKERAGE_EXPENSE),
                ("stt", "stt", JournalAccount.STT_EXPENSE),
                ("exchange_charges", "exc", JournalAccount.EXCHANGE_FEE_EXPENSE),
                ("gst", "gst", JournalAccount.GST_EXPENSE),
                ("stamp_duty", "stm", JournalAccount.STAMP_DUTY_EXPENSE),
            ]
            for key, code, acct in mapping:
                amt = Decimal(str(breakdown.get(key, "0.0000")))
                if amt > Decimal("0.0000"):
                    postings.append(
                        JournalPosting(entry_id=f"j_{code}_{fill_id}", fill_id=fill_id, ledger_account=acct, debit=amt)
                    )

        if not postings and fee > Decimal("0.0000"):
            postings.append(
                JournalPosting(
                    entry_id=f"j_fee_{fill_id}", fill_id=fill_id, ledger_account=JournalAccount.FEE_EXPENSE, debit=fee
                )
            )
        return postings

    @staticmethod
    def _insert_postings(c: _Cursor, postings: list[JournalPosting]) -> None:
        assert_balanced_journal(postings)
        for p in postings:
            c.run(
                """
                INSERT INTO journal_entries (entry_id, fill_id, capital_event_id, ledger_account, debit, credit, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (p.entry_id, p.fill_id, p.capital_event_id, str(p.ledger_account), p.debit, p.credit, p.created_at),
            )

    def process_fill(self, conn: Any, fill_data: dict[str, Any]) -> str:
        """
        Applies an execution fill atomically and idempotently.
        Enforces canonical lock ordering:
          1. Check fills (idempotency)
          2. Lock investor (investor_id)
          3. Lock holdings (investor_id, symbol)
          4. Lock order (order_id)
        """
        fill_id = str(fill_data["fill_id"])
        order_id = str(fill_data["order_id"])
        qty = int(fill_data["quantity"])
        price = _dec(fill_data["price"])
        fee = _dec(fill_data.get("fee", "0.0000"))
        cost_of_shares = qty * price
        total_cost = cost_of_shares + fee

        fee_breakdown = fill_data.get("fee_breakdown")
        if hasattr(fee_breakdown, "model_dump"):
            fee_breakdown = fee_breakdown.model_dump()

        with _Cursor(conn) as c:
            # 1. Idempotency Check (FR-6)
            if c.run("SELECT 1 FROM fills WHERE fill_id = %s FOR UPDATE", (fill_id,)).one():
                return "ALREADY_APPLIED"

            order_info = c.run(
                "SELECT investor_id, symbol, side, reserved_amount, status FROM orders WHERE order_id = %s",
                (order_id,),
            ).one()
            if not order_info:
                raise ValueError(f"ORDER_NOT_FOUND: {order_id}")

            investor_id = int(order_info["investor_id"])
            symbol = str(order_info["symbol"])
            side = str(order_info["side"])
            reserved_amount = _dec(order_info["reserved_amount"])

            # --- Canonical Deadlock-Free Lock Ordering ---
            inv = c.run(
                "SELECT wallet_balance, reserved_cash FROM investors WHERE investor_id = %s FOR UPDATE",
                (investor_id,),
            ).one()
            wallet_balance = _dec(inv["wallet_balance"])
            reserved_cash = _dec(inv["reserved_cash"])

            holding = c.run(
                "SELECT quantity, reserved_quantity, avg_buy_price FROM holdings "
                "WHERE investor_id = %s AND symbol = %s FOR UPDATE",
                (investor_id, symbol),
            ).one()

            locked = c.run("SELECT status FROM orders WHERE order_id = %s FOR UPDATE", (order_id,)).one()
            if str(locked["status"]) != str(OrderStatus.PENDING):
                raise OrderStateError(f"ORDER_NOT_FILLABLE: {order_id} is {locked['status']}, not PENDING")

            if side == "BUY":
                if reserved_amount > reserved_cash:
                    raise SolvencyViolationError(
                        f"Reservation release ({reserved_amount}) exceeds held cash ({reserved_cash})"
                    )
                new_balance = wallet_balance - total_cost
                new_reserved = reserved_cash - reserved_amount
                assert_account_solvency(new_balance, new_reserved)
                c.run(
                    "UPDATE investors SET wallet_balance = %s, reserved_cash = %s, updated_at = CURRENT_TIMESTAMP "
                    "WHERE investor_id = %s",
                    (new_balance, new_reserved, investor_id),
                )

                if holding:
                    curr_qty = int(holding["quantity"])
                    curr_avg = _dec(holding["avg_buy_price"])
                    new_qty = curr_qty + qty
                    new_avg = (((curr_qty * curr_avg) + cost_of_shares) / Decimal(new_qty)).quantize(
                        MONEY_Q, rounding=ROUND_HALF_UP
                    )
                    assert_holding_solvency(new_qty, int(holding["reserved_quantity"]))
                    c.run(
                        "UPDATE holdings SET quantity = %s, avg_buy_price = %s WHERE investor_id = %s AND symbol = %s",
                        (new_qty, new_avg, investor_id, symbol),
                    )
                else:
                    c.run(
                        "INSERT INTO holdings (investor_id, symbol, quantity, reserved_quantity, avg_buy_price) "
                        "VALUES (%s, %s, %s, 0, %s)",
                        (investor_id, symbol, qty, price),
                    )

                c.run(
                    "INSERT INTO fills (fill_id, order_id, quantity, price, fee, executed_at) "
                    "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)",
                    (fill_id, order_id, qty, price, fee),
                )
                postings = [
                    JournalPosting(entry_id=f"j_sec_{fill_id}", fill_id=fill_id,
                                   ledger_account=JournalAccount.SECURITIES_AT_COST, debit=cost_of_shares),
                    *self._generate_fee_postings(fill_id, fee, fee_breakdown),
                    JournalPosting(entry_id=f"j_csh_{fill_id}", fill_id=fill_id,
                                   ledger_account=JournalAccount.CASH, credit=total_cost),
                ]
                self._insert_postings(c, postings)

            elif side == "SELL":
                if not holding or int(holding["quantity"]) < qty:
                    held = int(holding["quantity"]) if holding else 0
                    raise InsufficientHoldingsError(
                        f"INSUFFICIENT_HOLDINGS: cannot settle SELL {qty} {symbol}; investor {investor_id} holds {held}"
                    )
                if int(holding["reserved_quantity"]) < qty:
                    raise InsufficientHoldingsError(
                        f"INSUFFICIENT_HOLDINGS: SELL {order_id} has only {holding['reserved_quantity']} "
                        f"reserved {symbol} shares; fill needs {qty}"
                    )
                net_proceeds = cost_of_shares - fee
                new_balance = wallet_balance + net_proceeds
                assert_account_solvency(new_balance, reserved_cash)
                c.run(
                    "UPDATE investors SET wallet_balance = %s, updated_at = CURRENT_TIMESTAMP WHERE investor_id = %s",
                    (new_balance, investor_id),
                )

                new_qty = int(holding["quantity"]) - qty
                new_reserved_qty = int(holding["reserved_quantity"]) - qty
                assert_holding_solvency(new_qty, new_reserved_qty)
                c.run(
                    "UPDATE holdings SET quantity = %s, reserved_quantity = %s WHERE investor_id = %s AND symbol = %s",
                    (new_qty, new_reserved_qty, investor_id, symbol),
                )

                c.run(
                    "INSERT INTO fills (fill_id, order_id, quantity, price, fee, executed_at) "
                    "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)",
                    (fill_id, order_id, qty, price, fee),
                )
                # Relieve inventory at average cost; the difference to gross proceeds is realized P&L.
                cost_basis = (Decimal(qty) * _dec(holding["avg_buy_price"])).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
                realized = cost_of_shares - cost_basis
                postings = [
                    JournalPosting(entry_id=f"j_csh_{fill_id}", fill_id=fill_id,
                                   ledger_account=JournalAccount.CASH, debit=net_proceeds),
                    *self._generate_fee_postings(fill_id, fee, fee_breakdown),
                    JournalPosting(entry_id=f"j_sec_{fill_id}", fill_id=fill_id,
                                   ledger_account=JournalAccount.SECURITIES_AT_COST, credit=cost_basis),
                ]
                if realized > 0:
                    postings.append(JournalPosting(entry_id=f"j_rgl_{fill_id}", fill_id=fill_id,
                                                   ledger_account=JournalAccount.REALIZED_GAIN_LOSS, credit=realized))
                elif realized < 0:
                    postings.append(JournalPosting(entry_id=f"j_rgl_{fill_id}", fill_id=fill_id,
                                                   ledger_account=JournalAccount.REALIZED_GAIN_LOSS, debit=-realized))
                self._insert_postings(c, postings)

            c.run("UPDATE orders SET status = %s WHERE order_id = %s", (str(OrderStatus.FILLED), order_id))

        return "APPLIED"

    # ------------------------------------------------------------------ order lifecycle

    def cancel_order(self, conn: Any, order_id: str, reason: Optional[str] = None) -> str:
        """PENDING -> CANCELED, releasing the cash (BUY) or share (SELL) reservation atomically."""
        return self._close_order(conn, order_id, OrderStatus.CANCELED, reason)

    def reject_order(self, conn: Any, order_id: str, reason: str) -> str:
        """PENDING -> REJECTED (e.g. broker refusal), releasing the reservation atomically."""
        return self._close_order(conn, order_id, OrderStatus.REJECTED, reason)

    def _close_order(self, conn: Any, order_id: str, terminal: OrderStatus, reason: Optional[str]) -> str:
        with _Cursor(conn) as c:
            o = c.run(
                "SELECT investor_id, symbol, side, quantity, reserved_amount FROM orders WHERE order_id = %s",
                (order_id,),
            ).one()
            if not o:
                raise ValueError(f"ORDER_NOT_FOUND: {order_id}")
            investor_id, symbol, side = int(o["investor_id"]), str(o["symbol"]), str(o["side"])

            # Canonical lock order: investor -> holding -> order.
            inv = c.run(
                "SELECT wallet_balance, reserved_cash FROM investors WHERE investor_id = %s FOR UPDATE", (investor_id,)
            ).one()
            h = None
            if side == "SELL":
                h = c.run(
                    "SELECT quantity, reserved_quantity FROM holdings WHERE investor_id = %s AND symbol = %s FOR UPDATE",
                    (investor_id, symbol),
                ).one()
            status = str(c.run("SELECT status FROM orders WHERE order_id = %s FOR UPDATE", (order_id,)).one()["status"])
            if status == str(terminal):
                return f"ALREADY_{terminal}"
            if status != str(OrderStatus.PENDING):
                raise OrderStateError(f"ILLEGAL_TRANSITION: {order_id} is {status}; cannot move to {terminal}")

            if side == "BUY":
                new_reserved = _dec(inv["reserved_cash"]) - _dec(o["reserved_amount"])
                assert_account_solvency(_dec(inv["wallet_balance"]), new_reserved)
                c.run(
                    "UPDATE investors SET reserved_cash = %s, updated_at = CURRENT_TIMESTAMP WHERE investor_id = %s",
                    (new_reserved, investor_id),
                )
            else:
                new_reserved_qty = int(h["reserved_quantity"]) - int(o["quantity"])
                assert_holding_solvency(int(h["quantity"]), new_reserved_qty)
                c.run(
                    "UPDATE holdings SET reserved_quantity = %s WHERE investor_id = %s AND symbol = %s",
                    (new_reserved_qty, investor_id, symbol),
                )
            c.run(
                "UPDATE orders SET status = %s, status_reason = %s WHERE order_id = %s",
                (str(terminal), reason, order_id),
            )
        return str(terminal)

    def record_rejected_order(self, conn: Any, order: OrderIntent, reason: str) -> None:
        """Persists an intent that failed validation as REJECTED (nothing reserved), keeping provenance."""
        with _Cursor(conn) as c:
            c.run(
                """
                INSERT INTO stocks (symbol, company_name, sector, current_price)
                VALUES (%s, %s, 'Equities', %s)
                ON CONFLICT (symbol) DO NOTHING
                """,
                (order.symbol, order.symbol, order.price),
            )
            c.run(
                """
                INSERT INTO orders (order_id, investor_id, symbol, side, quantity, price, reserved_amount,
                                    status, decision_ref, created_at, status_reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'REJECTED', %s, %s, %s)
                ON CONFLICT (order_id) DO NOTHING
                """,
                (
                    order.order_id, order.investor_id, order.symbol, str(order.side), order.quantity,
                    order.price, order.reserved_amount, order.decision_ref, order.created_at, reason[:255],
                ),
            )

    def get_order(self, order_id: str) -> Optional[dict[str, Any]]:
        rows = self._query("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        return rows[0] if rows else None

    def execute_order(self, order: OrderIntent, broker: Any) -> dict[str, Any]:
        """
        Intake -> broker -> settlement as ONE transaction, with explicit failure paths:
          - order id already known: no-op, returns its current status (replay-safe);
          - validation failure (funds/holdings): rolled back, then recorded as REJECTED;
          - broker refusal: reservation released and order REJECTED in the same transaction.
        """
        from glassledger.engine.broker import BrokerRejectedError

        existing = self.get_order(order.order_id)
        if existing:
            return {"status": str(existing["status"]), "replayed": True, "fill": None}

        with self.get_connection_context() as conn:
            try:
                with self.transaction(conn):
                    self.submit_order_intent(conn, order)
                    try:
                        fill = broker.submit_order(order)
                    except BrokerRejectedError as e:
                        self.reject_order(conn, order.order_id, f"BROKER_REJECTED: {e}")
                        return {"status": str(OrderStatus.REJECTED), "reason": f"BROKER_REJECTED: {e}", "fill": None}
                    self.process_fill(conn, {
                        "fill_id": fill.fill_id,
                        "order_id": fill.order_id,
                        "quantity": fill.quantity,
                        "price": str(fill.price),
                        "fee": str(fill.fee),
                        "fee_breakdown": fill.fee_breakdown,
                    })
                return {"status": str(OrderStatus.FILLED), "fill": fill}
            except SolvencyViolationError as e:
                with self.transaction(conn):
                    self.record_rejected_order(conn, order, str(e))
                return {"status": str(OrderStatus.REJECTED), "reason": str(e), "fill": None}

    # ------------------------------------------------------------------ read models

    def get_investor_account(self, investor_id: int = 1) -> dict[str, Any]:
        rows = self._query(
            "SELECT investor_id, name, email, wallet_balance, reserved_cash, updated_at FROM investors WHERE investor_id = %s",
            (investor_id,),
        )
        return rows[0] if rows else {}

    def get_holdings(self, investor_id: int = 1) -> list[dict[str, Any]]:
        """
        Retrieves inventory holdings with current market valuation and FR-11 Unrealized P&L.
        """
        return self._query(
            """
            SELECT h.symbol, h.quantity, h.reserved_quantity, h.avg_buy_price,
                   COALESCE(s.current_price, h.avg_buy_price) as current_price,
                   (h.quantity * COALESCE(s.current_price, h.avg_buy_price)) as current_valuation,
                   (h.quantity * (COALESCE(s.current_price, h.avg_buy_price) - h.avg_buy_price)) as unrealized_pnl
            FROM holdings h
            LEFT JOIN stocks s ON h.symbol = s.symbol
            WHERE h.investor_id = %s
            """,
            (investor_id,),
        )

    def get_pnl_summary(self, investor_id: int = 1) -> dict[str, Any]:
        """
        FR-11 Realized and Unrealized Profit & Loss (P&L) Calculator.
        - Unrealized: mark-to-market of open positions vs avg_buy_price.
        - Realized: SUM(credit - debit) of the investor's REALIZED_GAIN_LOSS journal lines
          (gross of fees; fees are separate *_EXPENSE lines).
        """
        total_unrealized = Decimal("0.0000")
        for h in self.get_holdings(investor_id):
            total_unrealized += _dec(h.get("unrealized_pnl") or "0.0000")
        total_unrealized = total_unrealized.quantize(MONEY_Q, rounding=ROUND_HALF_UP)
        row = self._query(
            """
            SELECT COALESCE(SUM(j.credit), 0) AS c, COALESCE(SUM(j.debit), 0) AS d
            FROM journal_entries j
            JOIN fills f ON f.fill_id = j.fill_id
            JOIN orders o ON o.order_id = f.order_id
            WHERE o.investor_id = %s AND j.ledger_account = 'REALIZED_GAIN_LOSS'
            """,
            (investor_id,),
        )[0]
        realized = (_dec(row["c"]) - _dec(row["d"])).quantize(MONEY_Q)
        return {
            "unrealized_pnl": total_unrealized,
            "realized_pnl": realized,
            "total_pnl": total_unrealized + realized,
        }

    def get_journal_summary(self) -> dict[str, Decimal]:
        row = self._query(
            "SELECT COALESCE(SUM(debit), 0) as total_debit, COALESCE(SUM(credit), 0) as total_credit FROM journal_entries"
        )[0]
        return {
            "total_debit": _dec(row["total_debit"]).quantize(Decimal("0.0001")),
            "total_credit": _dec(row["total_credit"]).quantize(Decimal("0.0001")),
        }

    def update_stock_price(self, symbol: str, price: Decimal) -> None:
        """Updates the latest market price of a stock for accurate mark-to-market valuation."""
        with self.get_connection_context() as conn:
            with _Cursor(conn) as c:
                c.run(
                    """
                    INSERT INTO stocks (symbol, company_name, sector, current_price)
                    VALUES (%s, %s, 'Equities', %s)
                    ON CONFLICT (symbol) DO UPDATE SET current_price = EXCLUDED.current_price
                    """,
                    (symbol, symbol, price),
                )
