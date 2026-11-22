-- GlassLedger migration 003: at most one journal line per account per source.
-- Makes the FD (fill_id, ledger_account) -> entry_id a declared key of journal_entries,
-- and the same for capital-event lines. NULLs are distinct in PostgreSQL UNIQUE
-- constraints, so lines from the other source are unaffected.
-- Existing duplicates are NOT deleted; the migration stops instead.

DO $$
DECLARE dup_fill INT;
DECLARE dup_cap INT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_journal_fill_account') THEN
        SELECT COUNT(*) INTO dup_fill FROM (
            SELECT fill_id, ledger_account FROM journal_entries
            WHERE fill_id IS NOT NULL
            GROUP BY fill_id, ledger_account HAVING COUNT(*) > 1
        ) d;
        IF dup_fill > 0 THEN
            RAISE EXCEPTION 'Cannot add uq_journal_fill_account: % (fill_id, ledger_account) pair(s) are duplicated. Resolve them manually, then re-run migrations.', dup_fill;
        END IF;
        ALTER TABLE journal_entries
            ADD CONSTRAINT uq_journal_fill_account UNIQUE (fill_id, ledger_account);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_journal_capital_account') THEN
        SELECT COUNT(*) INTO dup_cap FROM (
            SELECT capital_event_id, ledger_account FROM journal_entries
            WHERE capital_event_id IS NOT NULL
            GROUP BY capital_event_id, ledger_account HAVING COUNT(*) > 1
        ) d;
        IF dup_cap > 0 THEN
            RAISE EXCEPTION 'Cannot add uq_journal_capital_account: % (capital_event_id, ledger_account) pair(s) are duplicated. Resolve them manually, then re-run migrations.', dup_cap;
        END IF;
        ALTER TABLE journal_entries
            ADD CONSTRAINT uq_journal_capital_account UNIQUE (capital_event_id, ledger_account);
    END IF;
END $$;

-- The composite unique indexes lead with fill_id / capital_event_id, so the
-- single-column lookup indexes are now redundant.
DROP INDEX IF EXISTS idx_journal_fill_id;
DROP INDEX IF EXISTS idx_journal_capital_event_id;
