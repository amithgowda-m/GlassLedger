# Importing local candle files as GlassLedger market data

GlassLedger can replay real NSE 15-minute bars converted from **your own candle data** (not included in this repository). The source files are read-only and never committed:

| Path | Content | Git |
|---|---|---|
| `data/candles/` | `candles_{1m,5m,15m,1d}/symbol=<SYM>/period=<P>/part.parquet` | ignored |
| `data/reports/` | optional data-quality report (`quality_*.json`) listing special sessions, masked days and split/bonus ex-dates | ignored |
| `data/imports/` | converter outputs (`<dataset>.csv`, `<dataset>.report.json`) | ignored |

Requires the optional `data` extra: `pip install -e ".[data]"` (DuckDB).

## Expected candle schema
- **Columns:** `symbol`, `ts_utc_ns` (epoch nanoseconds, UTC), `trade_date` (IST session date), `open/high/low/close` (DOUBLE), `volume`, `oi`.
- **`ts_utc_ns` is the bar START.**
  - A normal 15m session is stamped 09:15 → 15:15 IST (25 bars).
  - A normal 1m session is stamped 09:15 → 15:29 IST (375 bars).
- **Prices are 2-decimal values stored as floats.** The converter turns them into exact decimal strings and refuses any value that is not a clean 2-dp price.

## Conversion rules (`src/glassledger/ingest/candles.py`)
- **15m bars are rebuilt from 1m.**
  - Buckets align to 09:15 IST. IST is UTC+5:30 = 22 × 15 min, so the bucket is `floor(ts / 15 min)`.
  - open = first 1m open; close = last 1m close; high/low = extremes; volume = sum.
- **Only regular-session bars** are kept: 1m bars 09:15–15:29 IST.
- **Excluded days**, taken from the latest `quality_*.json` report:
  - special sessions (e.g. Muhurat, Saturday sessions);
  - bad-print days;
  - masked symbol-days.
- **One tick per bar:**
  - price = close; volume = bar volume;
  - **timestamp = bar END in UTC** (start + 15 min, so 09:30–15:30 IST). A strategy never sees an unfinished bar.
- **Output** is sorted with no duplicate timestamps. Event ids are deterministic (`cdl_<SYM>_15m_<YYYYMMDDTHHMMZ>`), so re-importing is a no-op.
- **Splits and bonuses:** if the range contains an ex-date listed in the report (confirmed or price-only candidate), conversion is refused. Use `--split-policy before|after` to keep only one side, never a series that crosses the ex-date.
- **Gaps** are reported, not filled: short sessions, partial bars, zero-volume bars, and days present in daily candles but missing from 1m.

## Usage
```powershell
.venv\Scripts\python scripts\convert_candles.py verify SBIN --days 20
.venv\Scripts\python scripts\convert_candles.py convert RELIANCE 2025-01-01 2025-12-31 --dataset reliance_2025_15m
.venv\Scripts\glassledger dataset import data\imports\reliance_2025_15m.csv --name reliance_2025_15m
.venv\Scripts\glassledger stocks add <SYMBOL> --company "<Company Ltd.>" --exchange NSE --price <last close>
.venv\Scripts\glassledger run --dataset reliance_2025_15m --strategy rsi_v1 --run-id cdl_reliance_001
.venv\Scripts\glassledger reconcile --run-id cdl_reliance_001
```

`glassledger stocks add` is insert-only: an existing `stocks` row is never modified. Migration `004_stock_exchange.sql` adds the nullable `stocks.exchange` column (`NSE`/`BSE`); existing rows keep `NULL`.

## Verification results (development machine, local data)
- **Provided 15m bars vs rebuild from 1m:** 9 symbols × 20 random regular sessions = 4,500 bars, **0 mismatches** in open, high, low, close or volume, and no bars present on only one side.
- **RELIANCE 2025:**
  - 247 sessions, **6,175 ticks** (25 per session), from 2025-01-01 09:30 IST to 2025-12-31 15:30 IST;
  - excluded special sessions: 2025-02-01 and 2025-10-21;
  - no gaps, no partial bars, no zero-volume bars;
  - prices ₹1,142.00–₹1,579.10.
- **Import:** 6,175 inserted; a re-import inserted 0.
- **An `rsi_v1` run on that dataset:**
  - 6,175 ticks; 182 orders: 117 filled, 65 rejected (16 insufficient funds, 49 insufficient holdings);
  - realized P&L **+₹2,697.34** gross of fees (fees ₹2,375.25); unrealized +₹480.45 on 30 shares at the last bar close;
  - debits = credits; wallet = CASH ledger; reconcile passed (182 consistent chains).
- **Trace check:** a SELL CASH line traced back to the tick for the 09:15 IST bar of 2025-01-08, at ₹1,255.25. That equals the converted bar close and the last of the bucket's 15 source 1m closes.

## Data caveats
- **Adjusted vs raw prices:** check each symbol's history before converting it. RELIANCE's history is back-adjusted (no price jump across its 2017 and 2024 bonuses), while the quality report marks other symbols' corporate actions as raw (jump present). Inside 2025 there is no RELIANCE corporate action, so 2025 prices equal traded prices.
- **15m coverage:** only some symbols come with 15m files; RELIANCE does not, so its 15m bars always come from 1m.
- **Volume:** the quality report flags daily volume ≠ sum of 1m volume on many days (for RELIANCE, 462 days above 1%). The converter uses 1m volume.
- **Universe:** if your universe list is today's index membership applied to the past, results carry survivorship bias.
