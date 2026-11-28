-- GlassLedger migration 002: settlement correctness.
-- Never edit 001; every change here is idempotent so re-running is safe.

-- ---------------------------------------------------------------------------
-- A. Chart of accounts: ledger_accounts lookup replaces the inline CHECK list.
--    account_code -> account_type is a real FD, so it lives in its own table (3NF).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ledger_accounts (
    account_code VARCHAR(32) PRIMARY KEY,
    account_type VARCHAR(16) NOT NULL,
    CONSTRAINT ck_account_type CHECK (account_type IN ('ASSET', 'LIABILITY', 'EQUITY', 'INCOME', 'EXPENSE'))
);

INSERT INTO ledger_accounts (account_code, account_type) VALUES
    ('CASH', 'ASSET'),
    ('SECURITIES_AT_COST', 'ASSET'),
    ('REALIZED_GAIN_LOSS', 'INCOME'),
    ('FEE_EXPENSE', 'EXPENSE'),
    ('BROKERAGE_EXPENSE', 'EXPENSE'),
    ('STT_EXPENSE', 'EXPENSE'),
    ('EXCHANGE_FEE_EXPENSE', 'EXPENSE'),
    ('GST_EXPENSE', 'EXPENSE'),
    ('STAMP_DUTY_EXPENSE', 'EXPENSE')
ON CONFLICT (account_code) DO NOTHING;

DO $$
DECLARE c record;
BEGIN
    FOR c IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'journal_entries'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%ledger_account%'
    LOOP
        EXECUTE format('ALTER TABLE journal_entries DROP CONSTRAINT %I', c.conname);
    END LOOP;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_journal_ledger_account') THEN
        ALTER TABLE journal_entries
            ADD CONSTRAINT fk_journal_ledger_account
            FOREIGN KEY (ledger_account) REFERENCES ledger_accounts(account_code);
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- B. Owner capital is journaled. A journal line originates from exactly one of
--    a fill or a capital event, so investor_id is never duplicated onto journal_entries.
-- ---------------------------------------------------------------------------
INSERT INTO ledger_accounts (account_code, account_type) VALUES ('OWNER_EQUITY', 'EQUITY')
ON CONFLICT (account_code) DO NOTHING;

CREATE TABLE IF NOT EXISTS capital_events (
    event_id VARCHAR(64) PRIMARY KEY,
    investor_id INT NOT NULL REFERENCES investors(investor_id),
    event_type VARCHAR(24) NOT NULL,
    amount NUMERIC(18, 4) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_capital_event_type CHECK (event_type IN ('OPENING_BALANCE', 'CAPITAL_ADJUSTMENT')),
    CONSTRAINT ck_capital_amount_nonzero CHECK (amount <> 0),
    CONSTRAINT ck_opening_positive CHECK (event_type <> 'OPENING_BALANCE' OR amount > 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_capital_opening_per_investor
    ON capital_events (investor_id) WHERE event_type = 'OPENING_BALANCE';
CREATE INDEX IF NOT EXISTS idx_capital_events_investor ON capital_events (investor_id);

ALTER TABLE journal_entries ALTER COLUMN fill_id DROP NOT NULL;
ALTER TABLE journal_entries ADD COLUMN IF NOT EXISTS capital_event_id VARCHAR(64) REFERENCES capital_events(event_id);
CREATE INDEX IF NOT EXISTS idx_journal_capital_event_id ON journal_entries (capital_event_id);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_journal_single_source') THEN
        ALTER TABLE journal_entries ADD CONSTRAINT ck_journal_single_source
            CHECK ((fill_id IS NULL) <> (capital_event_id IS NULL));
    END IF;
END $$;

-- Backfill: databases created before 002 hold unjournaled starting cash. Post one
-- capital event per investor for the gap between wallet_balance and the CASH journal,
-- so SUM(CASH debit - credit) == wallet_balance holds from this migration onward.
INSERT INTO capital_events (event_id, investor_id, event_type, amount)
SELECT 'cap_backfill_' || i.investor_id,
       i.investor_id,
       CASE WHEN g.gap > 0 THEN 'OPENING_BALANCE' ELSE 'CAPITAL_ADJUSTMENT' END,
       g.gap
FROM investors i
CROSS JOIN LATERAL (
    SELECT i.wallet_balance - COALESCE((
        SELECT SUM(j.debit - j.credit)
        FROM journal_entries j
        JOIN fills f ON f.fill_id = j.fill_id
        JOIN orders o ON o.order_id = f.order_id
        WHERE o.investor_id = i.investor_id AND j.ledger_account = 'CASH'
    ), 0) AS gap
) g
WHERE g.gap <> 0
  AND NOT EXISTS (SELECT 1 FROM capital_events c WHERE c.investor_id = i.investor_id);

INSERT INTO journal_entries (entry_id, fill_id, capital_event_id, ledger_account, debit, credit)
SELECT 'j_csh_' || c.event_id, NULL, c.event_id, 'CASH', GREATEST(c.amount, 0), GREATEST(-c.amount, 0)
FROM capital_events c WHERE c.event_id LIKE 'cap_backfill_%'
ON CONFLICT (entry_id) DO NOTHING;

INSERT INTO journal_entries (entry_id, fill_id, capital_event_id, ledger_account, debit, credit)
SELECT 'j_eqt_' || c.event_id, NULL, c.event_id, 'OWNER_EQUITY', GREATEST(-c.amount, 0), GREATEST(c.amount, 0)
FROM capital_events c WHERE c.event_id LIKE 'cap_backfill_%'
ON CONFLICT (entry_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- C. Order lifecycle: CANCELED / REJECTED transitions record why.
-- ---------------------------------------------------------------------------
ALTER TABLE orders ADD COLUMN IF NOT EXISTS status_reason VARCHAR(255);

-- ---------------------------------------------------------------------------
-- D. One order per decision: decision_ref becomes a candidate key of orders.
--    Existing duplicates are NOT deleted automatically; the migration stops instead.
-- ---------------------------------------------------------------------------
DO $$
DECLARE dup_count INT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_orders_decision_ref') THEN
        SELECT COUNT(*) INTO dup_count FROM (
            SELECT decision_ref FROM orders GROUP BY decision_ref HAVING COUNT(*) > 1
        ) d;
        IF dup_count > 0 THEN
            RAISE EXCEPTION 'Cannot add uq_orders_decision_ref: % decision_ref value(s) are shared by several orders. Resolve them manually, then re-run migrations.', dup_count;
        END IF;
        ALTER TABLE orders ADD CONSTRAINT uq_orders_decision_ref UNIQUE (decision_ref);
    END IF;
END $$;

-- The unique constraint's own index replaces the plain lookup index from 001.
DROP INDEX IF EXISTS idx_orders_decision_ref;
