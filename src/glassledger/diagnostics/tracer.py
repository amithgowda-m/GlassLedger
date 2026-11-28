from typing import Any, Optional
from rich.console import Console
from rich.tree import Tree
from rich.panel import Panel

from glassledger.storage.mongo import MongoStorage
from glassledger.storage.postgres import PostgresStorage


class Tracer:
    def __init__(self, pg: Optional[PostgresStorage] = None, mongo: Optional[MongoStorage] = None):
        self.pg = pg or PostgresStorage()
        self.mongo = mongo or MongoStorage()

    def trace(self, journal_entry_id: str) -> dict[str, Any]:
        """
        Traverses backwards:
        Journal Entry -> Fill -> Order -> MongoDB Decision Snapshot -> Originating Tick
        """
        import sqlite3
        with self.pg.get_connection_context() as conn:
            placeholder = "?" if isinstance(conn, sqlite3.Connection) else "%s"
            cur = conn.cursor()
            cur.execute(

                f"""
                SELECT j.entry_id, j.ledger_account, j.debit, j.credit, j.created_at as journal_time,
                       f.fill_id, f.quantity, f.price as fill_price, f.fee, f.executed_at,
                       o.order_id, o.symbol, o.side, o.price as order_price, o.reserved_amount, o.decision_ref
                FROM journal_entries j
                JOIN fills f ON j.fill_id = f.fill_id
                JOIN orders o ON f.order_id = o.order_id
                WHERE j.entry_id = {placeholder}
                """,
                (journal_entry_id,),
            )
            row = cur.fetchone()

            if not row:
                cur.execute(
                    f"""
                    SELECT j.entry_id, j.ledger_account, j.debit, j.credit, j.created_at as journal_time,
                           f.fill_id, f.quantity, f.price as fill_price, f.fee, f.executed_at,
                           o.order_id, o.symbol, o.side, o.price as order_price, o.reserved_amount, o.decision_ref
                    FROM journal_entries j
                    JOIN fills f ON j.fill_id = f.fill_id
                    JOIN orders o ON f.order_id = o.order_id
                    WHERE j.fill_id = {placeholder} OR o.order_id = {placeholder}
                    LIMIT 1
                    """,
                    (journal_entry_id, journal_entry_id),
                )
                row = cur.fetchone()

            capital_row = None
            if not row:
                # Owner-capital lines (migration 002) originate from a capital event, not a fill.
                cur.execute(
                    f"""
                    SELECT j.entry_id, j.ledger_account, j.debit, j.credit, j.created_at AS journal_time,
                           c.event_id, c.investor_id, c.event_type, c.amount, c.created_at AS event_time
                    FROM journal_entries j
                    JOIN capital_events c ON c.event_id = j.capital_event_id
                    WHERE j.entry_id = {placeholder}
                    """,
                    (journal_entry_id,),
                )
                capital_row = cur.fetchone()
            cur.close()

        if capital_row:
            c = dict(capital_row)
            jt, ct = c["journal_time"], c["event_time"]
            return {
                "journal": {
                    "entry_id": c["entry_id"],
                    "account": c["ledger_account"],
                    "debit": str(c["debit"]),
                    "credit": str(c["credit"]),
                    "created_at": jt.isoformat() if hasattr(jt, "isoformat") else str(jt),
                },
                "capital_event": {
                    "event_id": c["event_id"],
                    "investor_id": c["investor_id"],
                    "event_type": c["event_type"],
                    "amount": str(c["amount"]),
                    "created_at": ct.isoformat() if hasattr(ct, "isoformat") else str(ct),
                },
            }

        if not row:
            return {"error": f"Journal entry or execution '{journal_entry_id}' not found in PostgreSQL ledger."}

        row_dict = dict(row)
        decision_ref = row_dict["decision_ref"]

        # Fetch MongoDB decision snapshot
        decision_doc = self.mongo.get_decision(decision_ref)

        jt = row_dict["journal_time"]
        et = row_dict["executed_at"]
        jt_str = jt.isoformat() if hasattr(jt, "isoformat") else str(jt)
        et_str = et.isoformat() if hasattr(et, "isoformat") else str(et)

        result = {
            "journal": {
                "entry_id": row_dict["entry_id"],
                "account": row_dict["ledger_account"],
                "debit": str(row_dict["debit"]),
                "credit": str(row_dict["credit"]),
                "created_at": jt_str,
            },
            "fill": {
                "fill_id": row_dict["fill_id"],
                "quantity": row_dict["quantity"],
                "price": str(row_dict["fill_price"]),
                "fee": str(row_dict["fee"]),
                "executed_at": et_str,
            },
            "order": {
                "order_id": row_dict["order_id"],
                "symbol": row_dict["symbol"],
                "side": row_dict["side"],
                "price": str(row_dict["order_price"]),
                "decision_ref": decision_ref,
            },
            "provenance": decision_doc,
        }
        return result


    def render_tree(self, trace_result: dict[str, Any], console: Optional[Console] = None) -> None:
        console = console or Console()
        if "error" in trace_result:
            console.print(f"[bold red]Trace Error:[/bold red] {trace_result['error']}")
            return

        j = trace_result["journal"]
        if "capital_event" in trace_result:
            ce = trace_result["capital_event"]
            root = Tree(
                f"[bold green]📦 GlassLedger Capital Provenance[/bold green] [dim]({j['entry_id']})[/dim]"
            )
            j_node = root.add(
                f"[bold yellow]💰 Journal Entry:[/bold yellow] [cyan]{j['account']}[/cyan] "
                f"(Debit: [bold green]₹{j['debit']}[/bold green], Credit: [bold red]₹{j['credit']}[/bold red])"
            )
            j_node.add(f"[dim]Timestamp: {j['created_at']}[/dim]")
            c_node = root.add(
                f"[bold blue]🏦 Capital Event:[/bold blue] [cyan]{ce['event_id']}[/cyan] "
                f"({ce['event_type']} of ₹{ce['amount']} for investor {ce['investor_id']})"
            )
            c_node.add("[dim]Owner capital movement; no market decision or tick is involved.[/dim]")
            console.print(Panel(root, title="[bold]Capital Audit Trail (FR-8)[/bold]", border_style="green"))
            return

        f = trace_result["fill"]
        o = trace_result["order"]
        p = trace_result.get("provenance") or {}
        tick = p.get("trigger_tick", {})
        indicators = p.get("indicator_state", {})

        root = Tree(
            f"[bold green]📦 GlassLedger Trade Provenance Lineage[/bold green] [dim]({j['entry_id']})[/dim]"
        )

        # 1. Financial Movement
        j_node = root.add(
            f"[bold yellow]💰 Journal Entry:[/bold yellow] [cyan]{j['account']}[/cyan] "
            f"(Debit: [bold green]₹{j['debit']}[/bold green], Credit: [bold red]₹{j['credit']}[/bold red])"
        )
        j_node.add(f"[dim]Timestamp: {j['created_at']}[/dim]")

        # 2. Execution Fill
        f_node = root.add(
            f"[bold blue]⚡ Execution Fill:[/bold blue] [cyan]{f['fill_id']}[/cyan] "
            f"({f['quantity']} shares @ [bold green]₹{f['price']}[/bold green], Fee: ₹{f['fee']})"
        )
        f_node.add(f"[dim]Settled at: {f['executed_at']}[/dim]")

        # 3. Order Intent
        o_node = root.add(
            f"[bold magenta]📋 Order Intent:[/bold magenta] [cyan]{o['order_id']}[/cyan] "
            f"([bold]{o['side']}[/bold] {f['quantity']} {o['symbol']} @ ₹{o['price']})"
        )
        o_node.add(f"[dim]Linked Decision Ref: {o['decision_ref']}[/dim]")

        # 4. Strategy VM Decision
        p_node = root.add(
            f"[bold red]🧠 Strategy VM Decision:[/bold red] [cyan]{p.get('decision_id', 'N/A')}[/cyan] "
            f"(Signal: [bold yellow]{p.get('signal', 'N/A')}[/bold yellow], Seq: #{p.get('evaluation_seq', 'N/A')})"
        )
        
        # Indicator details
        ind_str = ", ".join(f"{k}: [bold cyan]{v}[/bold cyan]" for k, v in indicators.items())
        p_node.add(f"📊 State Snapshot: {ind_str}")

        # 5. Raw Market Tick
        t_node = root.add(
            f"[bold white]⏱️ Originating Market Tick:[/bold white] [cyan]{tick.get('event_id', 'N/A')}[/cyan] "
            f"({tick.get('symbol', 'N/A')} @ [bold green]₹{tick.get('price', 'N/A')}[/bold green], "
            f"Vol: {tick.get('volume', 'N/A')}, Ts: {tick.get('timestamp', 'N/A')})"
        )

        console.print(Panel(root, title="[bold]Trade Audit Trail (FR-8)[/bold]", border_style="green"))
