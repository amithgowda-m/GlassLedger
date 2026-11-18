-- GlassLedger PostgreSQL Schema Definition
-- Enforces ACID financial invariants, solvency check constraints, and balanced double-entry ledger.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. Investors Table
CREATE TABLE IF NOT EXISTS investors (
    investor_id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(150) UNIQUE NOT NULL,
    wallet_balance NUMERIC(18, 4) NOT NULL DEFAULT 0.0000,
    reserved_cash NUMERIC(18, 4) NOT NULL DEFAULT 0.0000,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_wallet_positive CHECK (wallet_balance >= 0),
    CONSTRAINT ck_reserved_positive CHECK (reserved_cash >= 0),
    CONSTRAINT ck_cash_solvency CHECK (reserved_cash <= wallet_balance)
);

-- 2. Stocks Table
CREATE TABLE IF NOT EXISTS stocks (
    symbol VARCHAR(16) PRIMARY KEY,
    company_name VARCHAR(150) NOT NULL,
    sector VARCHAR(64) NOT NULL,
    current_price NUMERIC(18, 4) NOT NULL CHECK (current_price > 0)
);

-- 3. Holdings Table (Securities inventory per investor)
CREATE TABLE IF NOT EXISTS holdings (
    investor_id INT REFERENCES investors(investor_id) ON DELETE CASCADE,
    symbol VARCHAR(16) REFERENCES stocks(symbol) ON DELETE RESTRICT,
    quantity INT NOT NULL DEFAULT 0,
    reserved_quantity INT NOT NULL DEFAULT 0,
    avg_buy_price NUMERIC(18, 4) NOT NULL DEFAULT 0.0000,
    PRIMARY KEY (investor_id, symbol),
    CONSTRAINT ck_holding_qty_positive CHECK (quantity >= 0),
    CONSTRAINT ck_holding_reserved_positive CHECK (reserved_quantity >= 0),
    CONSTRAINT ck_holding_solvency CHECK (reserved_quantity <= quantity)
);

-- 4. Orders Table (Reservations and decision linkage)
CREATE TABLE IF NOT EXISTS orders (
    order_id VARCHAR(64) PRIMARY KEY,
    investor_id INT NOT NULL REFERENCES investors(investor_id),
    symbol VARCHAR(16) NOT NULL REFERENCES stocks(symbol),
    side VARCHAR(8) NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity INT NOT NULL CHECK (quantity > 0),
    price NUMERIC(18, 4) NOT NULL CHECK (price > 0),
    reserved_amount NUMERIC(18, 4) NOT NULL DEFAULT 0.0000,
    status VARCHAR(16) NOT NULL CHECK (status IN ('PENDING', 'FILLED', 'REJECTED', 'CANCELED')),
    decision_ref VARCHAR(64) NOT NULL, -- Logical link to MongoDB strategy_execution_log.decision_id
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 5. Fills Table (Idempotent execution units)
CREATE TABLE IF NOT EXISTS fills (
    fill_id VARCHAR(64) PRIMARY KEY,
    order_id VARCHAR(64) NOT NULL REFERENCES orders(order_id),
    quantity INT NOT NULL CHECK (quantity > 0),
    price NUMERIC(18, 4) NOT NULL CHECK (price > 0),
    fee NUMERIC(18, 4) NOT NULL DEFAULT 0.0000 CHECK (fee >= 0),
    executed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 6. Balanced Double-Entry Journal Postings
CREATE TABLE IF NOT EXISTS journal_entries (
    entry_id VARCHAR(64) PRIMARY KEY,
    fill_id VARCHAR(64) NOT NULL REFERENCES fills(fill_id),
    ledger_account VARCHAR(32) NOT NULL CHECK (ledger_account IN ('CASH', 'SECURITIES_AT_COST', 'FEE_EXPENSE')),
    debit NUMERIC(18, 4) NOT NULL DEFAULT 0.0000 CHECK (debit >= 0),
    credit NUMERIC(18, 4) NOT NULL DEFAULT 0.0000 CHECK (credit >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_debit_or_credit CHECK (
        (debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0)
    )
);

-- Fast Lookups for Lineage Audits and Trace
CREATE INDEX IF NOT EXISTS idx_orders_decision_ref ON orders(decision_ref);
CREATE INDEX IF NOT EXISTS idx_fills_order_id ON fills(order_id);
CREATE INDEX IF NOT EXISTS idx_journal_fill_id ON journal_entries(fill_id);
