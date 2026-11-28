-- GlassLedger migration 004: record the listing exchange of a stock.
-- Nullable on purpose: existing rows are left untouched (their exchange is unknown, and the
-- seeded AAPL row is not an NSE listing), new market-data symbols are registered with 'NSE'.

ALTER TABLE stocks ADD COLUMN IF NOT EXISTS exchange VARCHAR(16);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_stock_exchange') THEN
        ALTER TABLE stocks ADD CONSTRAINT ck_stock_exchange
            CHECK (exchange IS NULL OR exchange IN ('NSE', 'BSE'));
    END IF;
END $$;
