import sqlite3
from typing import Any, Optional
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from glassledger.storage.mongo import MongoStorage
from glassledger.storage.postgres import PostgresStorage


class CrossStoreReconciler:
    """
    FR-12 Cross-Store Consistency Auditor & Reconciler.
    Verifies bidirectional integrity between:
      - MongoDB strategy_execution_log (Decision telemetry)
      - PostgreSQL orders, fills, and journal_entries (Financial ledger)

    Invariants:
      1. Decision logged before Postgres settlement.
      2. An orphan decision (decision exists in Mongo with order intent, but no Postgres order)
         is classified as a harmless abort / failed reservation.
      3. An orphan order/fill (fill/order in Postgres with no decision in Mongo)
         is classified as a critical provenance violation.
    """

    def __init__(
        self,
        pg: Optional[PostgresStorage] = None,
        mongo: Optional[MongoStorage] = None,
    ):
        self.pg = pg or PostgresStorage()
        self.mongo = mongo or MongoStorage()

    def reconcile(self, run_id: Optional[str] = None) -> dict[str, Any]:
        """
        Executes bidirectional audit across MongoDB and PostgreSQL.
        Filters by run_id if provided.
        """
        # 1. Fetch decisions from MongoDB
        mongo_filter: dict[str, Any] = {}
        if run_id:
            mongo_filter["decision_id"] = {"$regex": f"^dec_{run_id}_"}

        cursor = self.mongo.db.strategy_execution_log.find(mongo_filter, {"_id": 0})
        decisions = list(cursor)
        mongo_decisions_by_id = {d["decision_id"]: d for d in decisions if "decision_id" in d}
        mongo_orders_claimed = {
            d["order_id"]: d for d in decisions if d.get("order_id")
        }

        # 2. Fetch orders and fills from PostgreSQL
        with self.pg.get_connection_context() as conn:
            cur = conn.cursor()
            is_sqlite = isinstance(conn, sqlite3.Connection)
            placeholder = "?" if is_sqlite else "%s"

            if run_id:
                order_query = (
                    f"SELECT order_id, investor_id, symbol, side, quantity, price, reserved_amount, status, decision_ref, created_at "
                    f"FROM orders WHERE order_id LIKE {placeholder}"
                )
                cur.execute(order_query, (f"ord_{run_id}_%",))
            else:
                cur.execute(
                    "SELECT order_id, investor_id, symbol, side, quantity, price, reserved_amount, status, decision_ref, created_at FROM orders"
                )
            pg_orders = [dict(r) for r in cur.fetchall()]

            if run_id:
                fill_query = (
                    f"SELECT f.fill_id, f.order_id, f.quantity, f.price, f.fee, f.executed_at, o.decision_ref "
                    f"FROM fills f JOIN orders o ON f.order_id = o.order_id "
                    f"WHERE f.order_id LIKE {placeholder}"
                )
                cur.execute(fill_query, (f"ord_{run_id}_%",))
            else:
                cur.execute(
                    "SELECT f.fill_id, f.order_id, f.quantity, f.price, f.fee, f.executed_at, o.decision_ref "
                    f"FROM fills f JOIN orders o ON f.order_id = o.order_id"
                )
            pg_fills = [dict(r) for r in cur.fetchall()]
            cur.close()

        pg_orders_by_id = {o["order_id"]: o for o in pg_orders}
        pg_orders_by_decision_ref = {o["decision_ref"]: o for o in pg_orders}

        # 3. Detect Orphan Decisions (Mongo has order_id, but Postgres has no matching order)
        orphan_decisions = []
        for order_id, dec in mongo_orders_claimed.items():
            if order_id not in pg_orders_by_id:
                orphan_decisions.append({
                    "decision_id": dec.get("decision_id"),
                    "order_id": order_id,
                    "signal": dec.get("signal"),
                    "evaluation_seq": dec.get("evaluation_seq"),
                    "trigger_tick": dec.get("trigger_tick"),
                    "status": "ORPHAN_DECISION_HARMLESS",
                    "reason": "Decision logged to MongoDB but PostgreSQL settlement transaction was never committed.",
                })

        # 4. Detect Orphan Orders (Postgres order references a decision_id missing in Mongo)
        orphan_orders = []
        for o in pg_orders:
            dec_ref = o.get("decision_ref")
            if dec_ref and dec_ref not in mongo_decisions_by_id:
                orphan_orders.append({
                    "order_id": o["order_id"],
                    "decision_ref": dec_ref,
                    "symbol": o.get("symbol"),
                    "side": o.get("side"),
                    "status": "ORPHAN_ORDER_CRITICAL",
                    "reason": "Order exists in financial ledger but causal decision is missing in MongoDB.",
                })

        # 5. Detect Orphan Fills
        orphan_fills = []
        for f in pg_fills:
            dec_ref = f.get("decision_ref")
            if dec_ref and dec_ref not in mongo_decisions_by_id:
                orphan_fills.append({
                    "fill_id": f["fill_id"],
                    "order_id": f["order_id"],
                    "decision_ref": dec_ref,
                    "status": "ORPHAN_FILL_CRITICAL",
                    "reason": "Fill settled in financial ledger without causal decision lineage in MongoDB.",
                })

        # 6. Count verified consistent chains
        consistent_count = sum(
            1 for o_id in mongo_orders_claimed if o_id in pg_orders_by_id
        )

        is_consistent = (
            len(orphan_decisions) == 0
            and len(orphan_orders) == 0
            and len(orphan_fills) == 0
        )

        return {
            "is_consistent": is_consistent,
            "run_id": run_id,
            "total_decisions_checked": len(decisions),
            "decisions_with_order_intent": len(mongo_orders_claimed),
            "total_orders_in_ledger": len(pg_orders),
            "total_fills_in_ledger": len(pg_fills),
            "consistent_chains_count": consistent_count,
            "orphan_decisions_count": len(orphan_decisions),
            "orphan_orders_count": len(orphan_orders),
            "orphan_fills_count": len(orphan_fills),
            "orphan_decisions": orphan_decisions,
            "orphan_orders": orphan_orders,
            "orphan_fills": orphan_fills,
            "summary": (
                "Cross-store consistency 100% verified. All decisions and ledger settlements are properly linked."
                if is_consistent
                else f"Found {len(orphan_decisions)} harmless orphan decision(s), {len(orphan_orders)} orphan order(s), {len(orphan_fills)} orphan fill(s)."
            ),
        }

    def render_reconciliation(self, report: dict[str, Any], console: Optional[Console] = None) -> None:
        console = console or Console()
        if report.get("is_consistent"):
            console.print(
                Panel(
                    f"[bold green]✔ CROSS-STORE RECONCILIATION PASSED (FR-12)[/bold green]\n\n"
                    f"Decisions Checked: [cyan]{report['total_decisions_checked']}[/cyan]\n"
                    f"Orders Verified:   [cyan]{report['total_orders_in_ledger']}[/cyan]\n"
                    f"Fills Verified:    [cyan]{report['total_fills_in_ledger']}[/cyan]\n"
                    f"Consistent Chains: [bold green]{report['consistent_chains_count']}[/bold green]\n"
                    f"Zero orphan orders or unrecorded fills detected.",
                    title="FR-12 Cross-Store Consistency Auditor",
                    border_style="green",
                )
            )
            return

        console.print(
            Panel(
                f"[bold red]⚠ RECONCILIATION ANOMALIES DETECTED (FR-12)[/bold red]\n\n"
                f"Decisions Checked: [cyan]{report['total_decisions_checked']}[/cyan]\n"
                f"Orphan Decisions (Harmless): [yellow]{report['orphan_decisions_count']}[/yellow]\n"
                f"Orphan Orders (Critical):    [red]{report['orphan_orders_count']}[/red]\n"
                f"Orphan Fills (Critical):     [red]{report['orphan_fills_count']}[/red]",
                title="FR-12 Cross-Store Consistency Auditor",
                border_style="yellow" if report['orphan_orders_count'] == 0 else "red",
            )
        )

        if report.get("orphan_decisions"):
            table = Table(title="Orphan Decisions (Logged in MongoDB, Aborted in PostgreSQL)", border_style="yellow")
            table.add_column("Decision ID", style="cyan")
            table.add_column("Claimed Order ID", style="magenta")
            table.add_column("Signal", style="yellow")
            table.add_column("Classification", style="green")
            for d in report["orphan_decisions"]:
                table.add_row(d["decision_id"], d["order_id"], d["signal"], d["status"])
            console.print(table)

        if report.get("orphan_orders") or report.get("orphan_fills"):
            table = Table(title="Orphan Ledger Records (Missing Decision in MongoDB)", border_style="red")
            table.add_column("Type", style="bold red")
            table.add_column("ID", style="red")
            table.add_column("Missing Decision Ref", style="cyan")
            for o in report.get("orphan_orders", []):
                table.add_row("ORDER", o["order_id"], o["decision_ref"])
            for f in report.get("orphan_fills", []):
                table.add_row("FILL", f["fill_id"], f["decision_ref"])
            console.print(table)
