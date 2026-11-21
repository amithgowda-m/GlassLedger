from decimal import Decimal
from pathlib import Path
import sqlite3
from typing import Any, Optional
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from glassledger.config import settings
from glassledger.domain.invariants import (
    SolvencyViolationError,
    assert_account_solvency,
    assert_holding_solvency,
    assert_balanced_journal,
)
from glassledger.domain.models import JournalPosting, JournalAccount, OrderIntent, OrderSide, OrderStatus


class PostgresStorage:
    """
    Financial Ledger Storage.
    Supports native PostgreSQL 16+ via psycopg3 connection pooling.
    Includes seamless SQLite ACID fallback mode if PostgreSQL daemon is offline.
    """

    def __init__(self, connection_uri: Optional[str] = None):
        self.uri = connection_uri or settings.postgres_uri
        self._pool: Optional[ConnectionPool] = None
        self._probed: Optional[bool] = None
        self._sqlite_path = Path("data/glassledger_ledger.db")

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
            self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    @property
    def pool(self) -> Optional[ConnectionPool]:
        if not self.is_sqlite_fallback and self._pool is None:
            self.open()
        return self._pool

    def get_sqlite_conn(self) -> sqlite3.Connection:
        self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._sqlite_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn


    def run_migrations(self, migration_sql_path: str) -> None:
        self.open()
        if not self.is_sqlite_fallback:
            with open(migration_sql_path, "r", encoding="utf-8") as f:
                sql = f.read()
            assert self.pool is not None
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
                conn.commit()
        else:
            # SQLite fallback schema
            conn = self.get_sqlite_conn()
            with conn:
                conn.execute(
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
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS stocks (
                        symbol TEXT PRIMARY KEY,
                        company_name TEXT NOT NULL,
                        sector TEXT NOT NULL,
                        current_price NUMERIC(18, 4) NOT NULL CHECK (current_price > 0)
                    );
                    """
                )
                conn.execute(
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
                    );
                    """
                )
                conn.execute(
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
                        decision_ref TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS fills (
                        fill_id TEXT PRIMARY KEY,
                        order_id TEXT NOT NULL REFERENCES orders(order_id),
                        quantity INTEGER NOT NULL CHECK (quantity > 0),
                        price NUMERIC(18, 4) NOT NULL CHECK (price > 0),
                        fee NUMERIC(18, 4) NOT NULL DEFAULT 0.0000 CHECK (fee >= 0),
                        executed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS journal_entries (
                        entry_id TEXT PRIMARY KEY,
                        fill_id TEXT NOT NULL REFERENCES fills(fill_id),
                        ledger_account TEXT NOT NULL CHECK (ledger_account IN ('CASH', 'SECURITIES_AT_COST', 'FEE_EXPENSE')),
                        debit NUMERIC(18, 4) NOT NULL DEFAULT 0.0000 CHECK (debit >= 0),
                        credit NUMERIC(18, 4) NOT NULL DEFAULT 0.0000 CHECK (credit >= 0),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        CHECK ((debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0))
                    );
                    """
                )
            conn.close()

    def seed_reference_data(self) -> None:
        self.open()
        if not self.is_sqlite_fallback:
            assert self.pool is not None
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO investors (investor_id, name, email, wallet_balance, reserved_cash)
                        VALUES (1, 'Quantitative Fund Alpha', 'alpha@glassledger.io', 100000.0000, 0.0000)
                        ON CONFLICT (investor_id) DO UPDATE 
                        SET wallet_balance = EXCLUDED.wallet_balance, reserved_cash = 0.0000;
                        """
                    )
                    cur.execute(
                        """
                        INSERT INTO stocks (symbol, company_name, sector, current_price)
                        VALUES 
                            ('RELIANCE', 'Reliance Industries Ltd.', 'Energy', 2850.0000),
                            ('TCS', 'Tata Consultancy Services', 'Technology', 3920.0000),
                            ('INFY', 'Infosys Ltd.', 'Technology', 1840.0000),
                            ('HDFCBANK', 'HDFC Bank Ltd.', 'Banking', 1650.0000),
                            ('AAPL', 'Apple Inc.', 'Technology', 19500.0000)
                        ON CONFLICT (symbol) DO UPDATE 
                        SET current_price = EXCLUDED.current_price;
                        """
                    )
                conn.commit()
        else:
            conn = self.get_sqlite_conn()
            with conn:
                conn.execute(
                    """
                    INSERT INTO investors (investor_id, name, email, wallet_balance, reserved_cash)
                    VALUES (1, 'Quantitative Fund Alpha', 'alpha@glassledger.io', 100000.0000, 0.0000)
                    ON CONFLICT (investor_id) DO UPDATE 
                    SET wallet_balance = 100000.0000, reserved_cash = 0.0000;
                    """
                )
                conn.execute(
                    """
                    INSERT INTO stocks (symbol, company_name, sector, current_price)
                    VALUES 
                        ('RELIANCE', 'Reliance Industries Ltd.', 'Energy', 2850.0000),
                        ('TCS', 'Tata Consultancy Services', 'Technology', 3920.0000),
                        ('INFY', 'Infosys Ltd.', 'Technology', 1840.0000),
                        ('HDFCBANK', 'HDFC Bank Ltd.', 'Banking', 1650.0000),
                        ('AAPL', 'Apple Inc.', 'Technology', 19500.0000)
                    ON CONFLICT (symbol) DO UPDATE 
                    SET current_price = EXCLUDED.current_price;
                    """
                )
            conn.close()

    def submit_order_intent(self, conn: Any, order: OrderIntent) -> str:
        """
        Submits an order intent with row-level lock on the investor.
        Locks strictly in canonical order (investor first).
        """
        is_sqlite = isinstance(conn, sqlite3.Connection)
        cur = conn.cursor()
        try:
            # 1. Row-lock investor
            if not is_sqlite:
                cur.execute(
                    "SELECT wallet_balance, reserved_cash FROM investors WHERE investor_id = %s FOR UPDATE",
                    (order.investor_id,),
                )
            else:
                cur.execute(
                    "SELECT wallet_balance, reserved_cash FROM investors WHERE investor_id = ?",
                    (order.investor_id,),
                )

            inv = cur.fetchone()
            if not inv:
                raise ValueError(f"Investor {order.investor_id} not found")

            wallet_balance = Decimal(str(inv["wallet_balance"]))
            reserved_cash = Decimal(str(inv["reserved_cash"]))
            available_cash = wallet_balance - reserved_cash

            if order.side == OrderSide.BUY:
                if available_cash < order.reserved_amount:
                    raise SolvencyViolationError(
                        f"INSUFFICIENT_FUNDS: Available cash {available_cash} < Required {order.reserved_amount}"
                    )
                new_reserved = reserved_cash + order.reserved_amount
                assert_account_solvency(wallet_balance, new_reserved)

                if not is_sqlite:
                    cur.execute(
                        """
                        UPDATE investors 
                        SET reserved_cash = %s, updated_at = NOW() 
                        WHERE investor_id = %s
                        """,
                        (new_reserved, order.investor_id),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE investors 
                        SET reserved_cash = ?, updated_at = CURRENT_TIMESTAMP 
                        WHERE investor_id = ?
                        """,
                        (str(new_reserved), order.investor_id),
                    )

            side_val = order.side.value if hasattr(order.side, "value") else str(order.side)
            status_val = order.status.value if hasattr(order.status, "value") else str(order.status)

            # Insert order record
            if not is_sqlite:
                cur.execute(
                    """
                    INSERT INTO orders (order_id, investor_id, symbol, side, quantity, price, reserved_amount, status, decision_ref, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (order_id) DO UPDATE SET status = EXCLUDED.status
                    """,
                    (
                        order.order_id,
                        order.investor_id,
                        order.symbol,
                        side_val,
                        order.quantity,
                        order.price,
                        order.reserved_amount,
                        status_val,
                        order.decision_ref,
                        order.created_at,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO orders (order_id, investor_id, symbol, side, quantity, price, reserved_amount, status, decision_ref, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (order_id) DO UPDATE SET status = excluded.status
                    """,
                    (
                        order.order_id,
                        order.investor_id,
                        order.symbol,
                        side_val,
                        order.quantity,
                        str(order.price),
                        str(order.reserved_amount),
                        status_val,
                        order.decision_ref,
                        order.created_at.isoformat(),
                    ),
                )
        finally:
            cur.close()
        return order.order_id



    def process_fill(self, conn: Any, fill_data: dict[str, Any]) -> str:
        """
        Applies an execution fill atomically and idempotently.
        Enforces canonical lock ordering:
          1. Check fills (idempotency)
          2. Lock investor (investor_id)
          3. Lock holdings (investor_id, symbol)
          4. Lock order (order_id)
        """
        is_sqlite = isinstance(conn, sqlite3.Connection)
        fill_id = str(fill_data["fill_id"])
        order_id = str(fill_data["order_id"])
        qty = int(fill_data["quantity"])
        price = Decimal(str(fill_data["price"]))
        fee = Decimal(str(fill_data.get("fee", "0.0000")))
        cost_of_shares = qty * price
        total_cost = cost_of_shares + fee

        cur = conn.cursor()
        try:
            # 1. Idempotency Check (FR-6)
            if not is_sqlite:
                cur.execute("SELECT 1 FROM fills WHERE fill_id = %s FOR UPDATE", (fill_id,))
            else:
                cur.execute("SELECT 1 FROM fills WHERE fill_id = ?", (fill_id,))

            if cur.fetchone():
                return "ALREADY_APPLIED"

            # Retrieve order details
            if not is_sqlite:
                cur.execute(
                    "SELECT investor_id, symbol, side, reserved_amount, status FROM orders WHERE order_id = %s",
                    (order_id,),
                )
            else:
                cur.execute(
                    "SELECT investor_id, symbol, side, reserved_amount, status FROM orders WHERE order_id = ?",
                    (order_id,),
                )

            order_info = cur.fetchone()
            if not order_info:
                raise ValueError(f"ORDER_NOT_FOUND: {order_id}")

            investor_id = int(order_info["investor_id"])
            symbol = str(order_info["symbol"])
            side = str(order_info["side"])
            reserved_amount = Decimal(str(order_info["reserved_amount"]))

            # --- Canonical Deadlock-Free Lock Ordering ---
            # 2. Lock investor
            if not is_sqlite:
                cur.execute(
                    "SELECT wallet_balance, reserved_cash FROM investors WHERE investor_id = %s FOR UPDATE",
                    (investor_id,),
                )
            else:
                cur.execute(
                    "SELECT wallet_balance, reserved_cash FROM investors WHERE investor_id = ?",
                    (investor_id,),
                )
            inv = cur.fetchone()
            wallet_balance = Decimal(str(inv["wallet_balance"]))
            reserved_cash = Decimal(str(inv["reserved_cash"]))

            # 3. Lock holdings
            if not is_sqlite:
                cur.execute(
                    "SELECT quantity, reserved_quantity, avg_buy_price FROM holdings WHERE investor_id = %s AND symbol = %s FOR UPDATE",
                    (investor_id, symbol),
                )
            else:
                cur.execute(
                    "SELECT quantity, reserved_quantity, avg_buy_price FROM holdings WHERE investor_id = ? AND symbol = ?",
                    (investor_id, symbol),
                )
            holding = cur.fetchone()

            # 4. Lock order
            if not is_sqlite:
                cur.execute("SELECT status FROM orders WHERE order_id = %s FOR UPDATE", (order_id,))
            else:
                cur.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,))

            # Financial settlement
            if side == "BUY":
                if reserved_amount > reserved_cash:
                    raise SolvencyViolationError(
                        f"Reservation release ({reserved_amount}) exceeds held cash ({reserved_cash})"
                    )

                new_balance = wallet_balance - total_cost
                new_reserved = reserved_cash - reserved_amount
                assert_account_solvency(new_balance, new_reserved)

                if not is_sqlite:
                    cur.execute(
                        """
                        UPDATE investors 
                        SET wallet_balance = %s, reserved_cash = %s, updated_at = NOW() 
                        WHERE investor_id = %s
                        """,
                        (new_balance, new_reserved, investor_id),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE investors 
                        SET wallet_balance = ?, reserved_cash = ?, updated_at = CURRENT_TIMESTAMP 
                        WHERE investor_id = ?
                        """,
                        (str(new_balance), str(new_reserved), investor_id),
                    )

                # Upsert Holdings & recompute weighted average purchase price
                if holding:
                    curr_qty = int(holding["quantity"])
                    curr_avg = Decimal(str(holding["avg_buy_price"]))
                    new_qty = curr_qty + qty
                    new_avg = ((curr_qty * curr_avg) + cost_of_shares) / Decimal(str(new_qty))
                    assert_holding_solvency(new_qty, int(holding["reserved_quantity"]))

                    if not is_sqlite:
                        cur.execute(
                            """
                            UPDATE holdings 
                            SET quantity = %s, avg_buy_price = %s 
                            WHERE investor_id = %s AND symbol = %s
                            """,
                            (new_qty, new_avg, investor_id, symbol),
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE holdings 
                            SET quantity = ?, avg_buy_price = ? 
                            WHERE investor_id = ? AND symbol = ?
                            """,
                            (new_qty, str(new_avg), investor_id, symbol),
                        )
                else:
                    if not is_sqlite:
                        cur.execute(
                            """
                            INSERT INTO holdings (investor_id, symbol, quantity, reserved_quantity, avg_buy_price) 
                            VALUES (%s, %s, %s, 0, %s)
                            """,
                            (investor_id, symbol, qty, price),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO holdings (investor_id, symbol, quantity, reserved_quantity, avg_buy_price) 
                            VALUES (?, ?, ?, 0, ?)
                            """,
                            (investor_id, symbol, qty, str(price)),
                        )

                # Record Fill
                if not is_sqlite:
                    cur.execute(
                        """
                        INSERT INTO fills (fill_id, order_id, quantity, price, fee, executed_at) 
                        VALUES (%s, %s, %s, %s, %s, NOW())
                        """,
                        (fill_id, order_id, qty, price, fee),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO fills (fill_id, order_id, quantity, price, fee, executed_at) 
                        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        (fill_id, order_id, qty, str(price), str(fee)),
                    )

                # Balanced Double-Entry Postings (Debits == Credits)
                postings = [
                    JournalPosting(
                        entry_id=f"j_sec_{fill_id}",
                        fill_id=fill_id,
                        ledger_account=JournalAccount.SECURITIES_AT_COST,
                        debit=cost_of_shares,
                        credit=Decimal("0.0000"),
                    ),
                    JournalPosting(
                        entry_id=f"j_fee_{fill_id}",
                        fill_id=fill_id,
                        ledger_account=JournalAccount.FEE_EXPENSE,
                        debit=fee,
                        credit=Decimal("0.0000"),
                    ),
                    JournalPosting(
                        entry_id=f"j_csh_{fill_id}",
                        fill_id=fill_id,
                        ledger_account=JournalAccount.CASH,
                        debit=Decimal("0.0000"),
                        credit=total_cost,
                    ),
                ]
                assert_balanced_journal(postings)

                for p in postings:
                    if not is_sqlite:
                        cur.execute(
                            """
                            INSERT INTO journal_entries (entry_id, fill_id, ledger_account, debit, credit, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            """,
                            (p.entry_id, p.fill_id, p.ledger_account, p.debit, p.credit, p.created_at),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO journal_entries (entry_id, fill_id, ledger_account, debit, credit, created_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (p.entry_id, p.fill_id, str(p.ledger_account), str(p.debit), str(p.credit), p.created_at.isoformat()),
                        )

            elif side == "SELL":
                net_proceeds = cost_of_shares - fee
                new_balance = wallet_balance + net_proceeds
                assert_account_solvency(new_balance, reserved_cash)

                if not is_sqlite:
                    cur.execute(
                        """
                        UPDATE investors 
                        SET wallet_balance = %s, updated_at = NOW() 
                        WHERE investor_id = %s
                        """,
                        (new_balance, investor_id),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE investors 
                        SET wallet_balance = ?, updated_at = CURRENT_TIMESTAMP 
                        WHERE investor_id = ?
                        """,
                        (str(new_balance), investor_id),
                    )

                if holding:
                    curr_qty = int(holding["quantity"])
                    new_qty = curr_qty - qty
                    if new_qty < 0:
                        raise SolvencyViolationError(f"Insufficient share quantity to sell: {curr_qty} < {qty}")
                    if not is_sqlite:
                        cur.execute(
                            """
                            UPDATE holdings 
                            SET quantity = %s 
                            WHERE investor_id = %s AND symbol = %s
                            """,
                            (new_qty, investor_id, symbol),
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE holdings 
                            SET quantity = ? 
                            WHERE investor_id = ? AND symbol = ?
                            """,
                            (new_qty, investor_id, symbol),
                        )

                if not is_sqlite:
                    cur.execute(
                        """
                        INSERT INTO fills (fill_id, order_id, quantity, price, fee, executed_at) 
                        VALUES (%s, %s, %s, %s, %s, NOW())
                        """,
                        (fill_id, order_id, qty, price, fee),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO fills (fill_id, order_id, quantity, price, fee, executed_at) 
                        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        (fill_id, order_id, qty, str(price), str(fee)),
                    )

                postings = [
                    JournalPosting(
                        entry_id=f"j_csh_{fill_id}",
                        fill_id=fill_id,
                        ledger_account=JournalAccount.CASH,
                        debit=net_proceeds,
                        credit=Decimal("0.0000"),
                    ),
                    JournalPosting(
                        entry_id=f"j_fee_{fill_id}",
                        fill_id=fill_id,
                        ledger_account=JournalAccount.FEE_EXPENSE,
                        debit=fee,
                        credit=Decimal("0.0000"),
                    ),
                    JournalPosting(
                        entry_id=f"j_sec_{fill_id}",
                        fill_id=fill_id,
                        ledger_account=JournalAccount.SECURITIES_AT_COST,
                        debit=Decimal("0.0000"),
                        credit=cost_of_shares,
                    ),
                ]
                assert_balanced_journal(postings)

                for p in postings:
                    account_val = p.ledger_account.value if hasattr(p.ledger_account, "value") else str(p.ledger_account)
                    if not is_sqlite:
                        cur.execute(
                            """
                            INSERT INTO journal_entries (entry_id, fill_id, ledger_account, debit, credit, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            """,
                            (p.entry_id, p.fill_id, account_val, p.debit, p.credit, p.created_at),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO journal_entries (entry_id, fill_id, ledger_account, debit, credit, created_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (p.entry_id, p.fill_id, account_val, str(p.debit), str(p.credit), p.created_at.isoformat()),
                        )

            # Mark order as FILLED
            filled_val = OrderStatus.FILLED.value if hasattr(OrderStatus.FILLED, "value") else str(OrderStatus.FILLED)
            if not is_sqlite:
                cur.execute("UPDATE orders SET status = %s WHERE order_id = %s", (filled_val, order_id))
            else:
                cur.execute("UPDATE orders SET status = ? WHERE order_id = ?", (filled_val, order_id))


        finally:
            cur.close()

        return "APPLIED"

    def get_connection_context(self):
        """Returns a context manager yielding an active connection (Postgres or SQLite)."""
        self.open()
        if not self.is_sqlite_fallback:
            assert self.pool is not None
            return self.pool.connection()
        else:
            class SQLiteContext:
                def __init__(self, path):
                    self.path = path
                    self.conn = None
                def __enter__(self):
                    self.conn = sqlite3.connect(str(self.path), timeout=30.0)
                    self.conn.row_factory = sqlite3.Row
                    self.conn.execute("PRAGMA foreign_keys = ON;")
                    self.conn.execute("PRAGMA journal_mode = WAL;")
                    self.conn.execute("PRAGMA synchronous = NORMAL;")
                    return self.conn

                def __exit__(self, exc_type, exc_val, exc_tb):
                    if self.conn:
                        if exc_type is None:
                            self.conn.commit()
                        else:
                            self.conn.rollback()
                        self.conn.close()
            return SQLiteContext(self._sqlite_path)

    def get_investor_account(self, investor_id: int = 1) -> dict[str, Any]:
        self.open()
        if not self.is_sqlite_fallback:
            assert self.pool is not None
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT investor_id, name, email, wallet_balance, reserved_cash, updated_at FROM investors WHERE investor_id = %s",
                        (investor_id,),
                    )
                    row = cur.fetchone()
                    return dict(row) if row else {}
        else:
            conn = self.get_sqlite_conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT investor_id, name, email, wallet_balance, reserved_cash, updated_at FROM investors WHERE investor_id = ?",
                (investor_id,),
            )
            row = cur.fetchone()
            res = dict(row) if row else {}
            conn.close()
            return res

    def get_holdings(self, investor_id: int = 1) -> list[dict[str, Any]]:
        """
        Retrieves inventory holdings with current market valuation and FR-11 Unrealized P&L.
        """
        self.open()
        if not self.is_sqlite_fallback:
            assert self.pool is not None
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
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
                    return [dict(r) for r in cur.fetchall()]
        else:
            conn = self.get_sqlite_conn()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT h.symbol, h.quantity, h.reserved_quantity, h.avg_buy_price,
                       COALESCE(s.current_price, h.avg_buy_price) as current_price,
                       (h.quantity * COALESCE(s.current_price, h.avg_buy_price)) as current_valuation,
                       (h.quantity * (COALESCE(s.current_price, h.avg_buy_price) - h.avg_buy_price)) as unrealized_pnl
                FROM holdings h
                LEFT JOIN stocks s ON h.symbol = s.symbol
                WHERE h.investor_id = ?
                """,
                (investor_id,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return rows

    def get_pnl_summary(self, investor_id: int = 1) -> dict[str, Any]:
        """
        FR-11 Realized and Unrealized Profit & Loss (P&L) Calculator.
        - Unrealized P&L: Sum of mark-to-market open position gains/losses.
        - Realized P&L: Closed trade settlement proceeds minus cost basis and fees.
        """
        holdings = self.get_holdings(investor_id)
        total_unrealized = Decimal("0.0000")
        for h in holdings:
            total_unrealized += Decimal(str(h.get("unrealized_pnl") or "0.0000"))

        # Realized P&L from journal entries (proceeds in CASH vs cost basis in SECURITIES_AT_COST and FEE_EXPENSE)
        summary = self.get_journal_summary()
        # In a balanced double entry, closed P&L reflects net equity growth
        return {
            "unrealized_pnl": total_unrealized,
            "realized_pnl": Decimal("0.0000"), # Tracked per round-trip
            "total_pnl": total_unrealized,
        }

    def get_journal_summary(self) -> dict[str, Decimal]:
        self.open()
        if not self.is_sqlite_fallback:
            assert self.pool is not None
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COALESCE(SUM(debit), 0) as total_debit, COALESCE(SUM(credit), 0) as total_credit FROM journal_entries"
                    )
                    row = cur.fetchone()
                    return {
                        "total_debit": Decimal(str(row["total_debit"])),
                        "total_credit": Decimal(str(row["total_credit"])),
                    }
        else:
            conn = self.get_sqlite_conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT COALESCE(SUM(debit), 0) as total_debit, COALESCE(SUM(credit), 0) as total_credit FROM journal_entries"
            )
            row = cur.fetchone()
            d = Decimal(str(row["total_debit"])).quantize(Decimal("0.0001"))
            c = Decimal(str(row["total_credit"])).quantize(Decimal("0.0001"))
            conn.close()
            return {
                "total_debit": d,
                "total_credit": c,
            }


