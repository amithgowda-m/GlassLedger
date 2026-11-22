# GlassLedger 🔍🏛️

**Paper-Trading Engine with a Double-Entry Settlement Ledger**  
*Deterministic Replay • Causal Provenance • Polyglot Persistence • Crash Safety*

---

## Overview

**GlassLedger** is a paper-trading (simulated money) engine that makes every trade explainable. It turns the "black box" of automated trading into a "glass box": each ledger line can be traced back to the market tick and strategy decision that caused it.

1. **Polyglot Persistence**: **PostgreSQL 16** owns the financial ledger (row locks, CHECK constraints, balanced double-entry journal). **MongoDB 7** owns market ticks (time-series collection) and decision telemetry (`strategy_execution_log`).
2. **Pure-Function Strategy VM**: strategies are pure state transitions $S_{t+1} = f(S_t, T_t, P)$, with no clock or randomness. Built-in strategies are an RSI threshold and an MA crossover. Ledger and strategy arithmetic uses `decimal.Decimal`. The exceptions are XIRR (computed in `float`) and the SQLite fallback (stores money as `REAL`; see below).
3. **5-Layer Causal Traceability (FR-8)**: journal entry → fill → order → strategy decision → originating market tick.
4. **Differential Diagnostic Engine (FR-9)**: aligns two runs by sequence number and classifies divergence as `INPUT`, `OBSERVATION`, `DECISION` or `SETTLEMENT`.
5. **Crash Safety (FR-10)**: settlement is one database transaction. A process killed mid-transaction leaves no partial state on PostgreSQL; this is tested by subprocess-kill tests that need a live PostgreSQL.
6. **Cross-Store Write Ordering & Reconciler (FR-12)**: the MongoDB decision is written *before* the PostgreSQL settlement commits. `CrossStoreReconciler` (`glassledger reconcile`) classifies harmless orphan decisions vs critical orphan ledger rows.
7. **Statutory Fee Breakdown (FR-13)**: brokerage, STT, exchange charges, GST and stamp duty are computed per fill and posted as separate expense lines, with $\sum\text{Debits} = \sum\text{Credits}$.
8. **Virtual Wallet & Portfolio**: NAV, unit accounting (10,000 base units), XIRR, mark-to-market holdings, realized/unrealized P&L, and one-click paper trades from the web UI.
9. **Broker & Market-Data Seams**: `PaperBroker` (deterministic slippage plus the fee model) is the working broker. `KiteBroker`, `ShoonyaBroker` and `DhanBroker` are **stubs**: they apply each broker's fee schedule locally and do **not** call any broker API. Market data: CSV replay and a Yahoo Finance HTTP poll work; the Breeze and Kite WebSocket sources are **stubs**.

---

## Settlement rules

| Event | Ledger effect |
|---|---|
| Investor created / seeded | Starting capital is journaled: `CASH` Dr / `OWNER_EQUITY` Cr (a `capital_events` row). |
| BUY intake | Cash reserved (`reserved_cash += reserved_amount`) under a row lock on the investor. |
| SELL intake | Refused unless the investor holds enough **unreserved** shares; otherwise `holdings.reserved_quantity += qty` under a row lock on the holding. |
| BUY fill | Reservation released; `SECURITIES_AT_COST` Dr, fee expenses Dr, `CASH` Cr; weighted-average cost updated (fees excluded, 4 dp half-up). |
| SELL fill | Share reservation released; `CASH` Dr net proceeds, fee expenses Dr, `SECURITIES_AT_COST` Cr at **average cost**, difference to `REALIZED_GAIN_LOSS`. |
| Cancel / reject | `PENDING → CANCELED/REJECTED` releases the cash or share reservation in the same transaction and records `status_reason`. |
| Validation failure (funds/holdings) | Order recorded as `REJECTED`; the strategy run continues. |
| Re-running a run id / replaying a fill | No second effect (`execute_order` and `process_fill` are idempotent). |

Invariants kept exactly:
- `wallet_balance` = Σ CASH journal (debits − credits);
- `reserved_cash` = Σ PENDING BUY reservations;
- `reserved_quantity` = Σ PENDING SELL quantity;
- realized P&L = Σ `REALIZED_GAIN_LOSS` lines (gross of fees).

See [`docs/normalization.md`](docs/normalization.md) for the schema, normalization (3NF/BCNF) and the full invariant list.

Schema changes live in `migrations/postgres/` (`001_initial_schema.sql`, `002_settlement_correctness.sql`) and are applied in order by `glassledger db migrate`.

---

## Architecture

```
                ┌─────────────────────────────────────────────────────────┐
                │                  MarketDataSource Seam                  │
                │  CSVReplaySource │ YFinancePoll │ Breeze* │ Kite*       │
                └────────────────────────────┬────────────────────────────┘
                                             │ Standardized Ticks
                                             ▼
                ┌─────────────────────────────────────────────────────────┐
                │           Monotonic Sequencer & Normalizer              │
                └─────────────┬─────────────────────────────┬─────────────┘
                              │                             │
                              ▼                             ▼
               ┌──────────────────────────────┐ ┌───────────────────────────┐
               │      MongoDB 7 (Telemetry)   │ │  Pure Strategy VM (FR-7)  │
               │  - price_history (TS)        │ │  evaluate(state,tick,prms)│
               │  - strategy_execution_log    │ │  -> (state, signal, snap) │
               │  - strategies ($jsonSchema)  │ └───────────┬───────────────┘
               └──────────────────────────────┘             │ Signal (decision logged first)
                                                            ▼
                                                ┌───────────────────────────┐
                                                │    BrokerAdapter Seam     │
                                                │ Paper │ Kite* Shoonya* Dhan*│
                                                └───────────┬───────────────┘
                                                            │ FillExecution
                                                            ▼
                                                ┌───────────────────────────┐
                                                │   PostgreSQL 16 (ACID)    │
                                                │ - Row Locks (FOR UPDATE)  │
                                                │ - Cash & Share Reservation│
                                                │ - Double-Entry Ledger     │
                                                │ - Idempotency (fill_id)   │
                                                └───────────────────────────┘
                                                        * = stub, no live API
```

---

## Quickstart

### 1. Environment Setup
```bash
git clone https://github.com/amithgowda-m/GlassLedger.git
cd GlassLedger

python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### 2. Start Databases
```bash
docker compose up -d        # postgres:16-alpine and mongo:7.0
cp .env.example .env
glassledger db migrate      # applies every migrations/postgres/*.sql in order + Mongo collections
```

### ⚠️ SQLite fallback (non-authoritative)
If PostgreSQL or MongoDB is unreachable, GlassLedger falls back to SQLite files in `data/` and logs a loud `NON-AUTHORITATIVE` warning. The fallback keeps the same tables, keys and constraints, but:
- money is stored as binary floating point (SQLite `NUMERIC` → `REAL`);
- there are no `SELECT … FOR UPDATE` row locks, so concurrent solvency guarantees do not hold.

Use it for demos only. A fallback file from an older schema version is renamed to `*.legacy-*.db` and a fresh one is created.

### 3. Run a Strategy Simulation
```bash
glassledger run --dataset baseline_ticks --strategy rsi_v1 --run-id run_001
glassledger replay run_001 --verify
glassledger trace j_csh_fill_ord_run_001_000015
glassledger reconcile
```

### 4. Launch the Web Console
```bash
glassledger ui --port 8000
```
Open **`http://127.0.0.1:8000`**. The console has five tabs: Dashboard, Ledger, Market Data, Strategy Studio and Simulations. Simulations has five sub-tabs: Runner, Diff, Reconciler, Crash Lab and Database Inspector.

---

## Tests

```bash
pytest -v
```

Tests never touch your data. Each session uses a fresh temp directory for SQLite and a separate `glassledger_test` database in MongoDB and PostgreSQL. Those test databases are dropped and recreated at session start.

What the suite covers:

- **Settlement**: SELL validation, share reservation, SELL cost-basis / realized P&L accounting, journaled opening capital, cancel/reject lifecycle, UNIQUE decision_ref, idempotent replays (`test_settlement.py`).
- **Ledger invariants**: stored balances recomputed from orders, fills and journal lines (`test_denormalization_invariants.py`).
- **Schema**: the migrations, the live PostgreSQL catalog and the SQLite fallback agree with `docs/normalization.md` (`test_schema_normalization.py`).
- **Determinism, diff engine, fee model, idempotency, reconciler, portfolio/XIRR**.

**PostgreSQL-only tests.** These skip automatically when PostgreSQL is not running on 5432:
- concurrent `SELECT … FOR UPDATE` tests (cash and shares);
- the named `ck_holding_solvency` check;
- the exact-decimal money test;
- the subprocess-kill crash tests;
- the live catalog checks.

Without PostgreSQL, the settlement and invariant tests still run on the SQLite fallback, but there they verify logic only, not decimal precision or locking.

---

## License

MIT License.
