# GlassLedger 🔍🏛️

**Institutional-Grade Algorithmic Trading & Double-Entry Settlement Engine**  
*Deterministic Replay • 5-Layer Causal Provenance • Polyglot Persistence • Crash Safety*

---

## Overview

**GlassLedger** is an algorithmic trading and settlement engine designed to convert the traditional "black box" of automated trading into a transparent "glass box".

1. **Polyglot Persistence**: **PostgreSQL 16+** owns the ACID financial ledger (row locks, check constraints, double-entry balanced journal). **MongoDB 8+** owns high-throughput market ticks (time-series collections) and polymorphic decision telemetry (`strategy_execution_log`).
2. **Pure-Function Strategy VM**: Pure mathematical state transitions $S_{t+1} = f(S_t, T_t, P)$ with zero clock drift, zero pseudo-randomness, and a **strict zero-float policy** (`decimal.Decimal` everywhere).
3. **5-Layer Causal Traceability (FR-8)**: Reconstructs the complete lineage of any ledger entry backwards:
   $$\text{Financial Ledger Entry} \longrightarrow \text{Fill Execution} \longrightarrow \text{Order Intent} \longrightarrow \text{Strategy Decision} \longrightarrow \text{Originating Market Tick}$$
4. **Differential Diagnostic Engine (FR-9)**: Aligns separate simulation runs strictly by sequence number and classifies divergence into `INPUT`, `OBSERVATION`, `DECISION`, or `SETTLEMENT`.
5. **Gold-Standard OS Crash Recovery (FR-10)**: Survives brutal mid-transaction termination (`kill -9` / `proc.kill()`) with zero orphaned orders and 100% database rollback.
6. **Cross-Store Write Ordering & Reconciler (FR-12)**: Enforces write ordering where MongoDB decision snapshots are permanently written BEFORE PostgreSQL settlement commits. The `CrossStoreReconciler` detects and classifies harmless orphan decisions vs critical orphan ledger entries.
7. **Statutory Multi-Line Fee Breakdown (FR-13)**: Computes and journals realistic regulatory transaction fees (Brokerage, STT, Exchange Charges, GST, Stamp Duty) with individual ledger expense postings while maintaining $\sum\text{Debits} == \sum\text{Credits}$.
8. **Virtual Investment Portfolio & Virtual Wallet**: Institutional-grade portfolio tracker computing NAV per unit, number of units (10,000 base), Newton-Raphson XIRR, and mark-to-market holdings. Execute 1-click virtual paper trades directly from the Live Market Data tab without actual financial commitments.
9. **Extensible Broker Seams**: Out-of-the-box support for `PaperBroker`, `ShoonyaBroker` (Finvasia 100% free API + free WebSocket), `DhanBroker`, and `KiteBroker` (Zerodha).

---

## Architecture

```
                ┌─────────────────────────────────────────────────────────┐
                │                  MarketDataSource Seam                  │
                │     CSVReplaySource │ YFinancePoll │ Breeze │ Kite      │
                └────────────────────────────┬────────────────────────────┘
                                             │ Standardized Ticks
                                             ▼
                ┌─────────────────────────────────────────────────────────┐
                │           Monotonic Sequencer & Normalizer             │
                │             (Assigns monotonic seq numbers)             │
                └─────────────┬─────────────────────────────┬─────────────┘
                              │                             │
                              ▼                             ▼
               ┌──────────────────────────────┐ ┌───────────────────────────┐
               │    MongoDB 8.2 (Telemetry)   │ │  Pure Strategy VM (FR-7)  │
               │  - price_history (TS)        │ │  evaluate(state,tick,prms)│
               │  - strategy_execution_log    │ │  -> (state, signal, snap) │
               │  - strategies ($jsonSchema)  │ │  (Zero clock / RNG / drift)│
               └──────────────────────────────┘ └───────────┬───────────────┘
                                                            │ Signal Generated
                                                            ▼
                                                ┌───────────────────────────┐
                                                │    BrokerAdapter Seam     │
                                                │  Paper │ Shoonya │ Dhan   │
                                                └───────────┬───────────────┘
                                                            │ FillExecution
                                                            ▼
                                                ┌───────────────────────────┐
                                                │   PostgreSQL 16.8 (ACID)  │
                                                │ - Row Locks (FOR UPDATE)  │
                                                │ - Reservation Accounting  │
                                                │ - Double-Entry Ledger     │
                                                │ - Idempotency (fill_id)   │
                                                └───────────────────────────┘
```

---

## Quickstart

### 1. Environment Setup
```bash
git clone https://github.com/amithgowda-m/GlassLedger.git
cd GlassLedger

python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e .
```

### 2. Configure Databases
Ensure PostgreSQL and MongoDB are running, then copy the example configuration:
```bash
cp .env.example .env
```

Apply database migrations:
```bash
glassledger db migrate
```

### 3. Run Strategy Simulation
```bash
# Execute baseline run
glassledger run --dataset baseline_ticks --strategy rsi_v1 --run-id run_001

# Replay and verify 100% bit-for-bit mathematical parity
glassledger replay run_001 --verify

# Trace a financial transaction to its originating market tick
glassledger trace j_csh_fill_ord_run_001_000015
```

### 4. Launch Visual Web Console
```bash
glassledger ui --port 8000
```
Open **`http://127.0.0.1:8000`** in your browser to access the 10 interactive console tabs:
- 💼 Virtual Investment Portfolio & Wallet (NAV, Units, XIRR, MTM Holdings)
- 📊 Executive Solvency Dashboard & FR-11 P&L
- 💰 Double-Entry Journal & 5-Layer Trace Tree (FR-8)
- ⚖️ Cross-Store Reconciler (FR-12 Write Order Auditor)
- 🔬 Differential Diagnostic Engine (FR-9)
- 📡 Live Real-Time Market Data & Virtual Trading (NSE Equities)
- 🧪 Strategy Studio (Tier 2 AST Security Analyzer)
- 🛡️ Crash Safety Lab (FR-10 Live Subprocess SIGKILL)
- 🗄️ Dual-Store Database Inspector
- ⚡ Simulation Runner

---

## Test Suite Verification

Run the full automated verification suite:
```bash
pytest -v
```

All 31 active tests pass across:
- **Crash Recovery**: Real subprocess OS-level `kill -9` rollback test.
- **Determinism**: Zero-drift bit-for-bit indicator reproducibility.
- **Diff Engine**: 4-way classification (`INPUT`, `OBSERVATION`, `DECISION`, `SETTLEMENT`).
- **Idempotency**: Duplicate fill delivery returns `ALREADY_APPLIED`.
- **Solvency**: 10-thread concurrent `SELECT ... FOR UPDATE` row-locking tests.
- **FR-12 Reconciler**: Cross-store write ordering and orphan decision audit with failure injection.
- **FR-13 Statutory Fees**: Realistic multi-line regulatory charges and balanced double-entry journal postings.
- **Virtual Portfolio**: Unit-accounting, NAV valuation, Newton-Raphson XIRR, and virtual trade updates.

---

## License

MIT License. Designed for quantitative researchers, algorithmic prop desks, and financial software engineers.
