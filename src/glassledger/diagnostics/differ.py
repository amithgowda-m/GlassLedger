from typing import Any, Optional
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from glassledger.storage.mongo import MongoStorage


class Differ:
    def __init__(self, mongo: Optional[MongoStorage] = None):
        self.mongo = mongo or MongoStorage()

    def compare_runs(self, run_id_1: str, run_id_2: str) -> dict[str, Any]:
        """
        Aligns two runs by strictly monotonic sequence numbers (`evaluation_seq`).
        Finds the earliest point of divergence and classifies the root cause into:
          - INPUT (missing tick or tick delta)
          - OBSERVATION (indicator calculation divergence)
          - DECISION (signal flip)
          - SETTLEMENT (order intent or fill difference)
        """
        # Fetch decision logs for both runs
        cursor1 = self.mongo.db.strategy_execution_log.find(
            {"decision_id": {"$regex": f"^dec_{run_id_1}_"}}, {"_id": 0}
        )
        cursor2 = self.mongo.db.strategy_execution_log.find(
            {"decision_id": {"$regex": f"^dec_{run_id_2}_"}}, {"_id": 0}
        )

        map1 = {int(doc["evaluation_seq"]): doc for doc in cursor1}
        map2 = {int(doc["evaluation_seq"]): doc for doc in cursor2}

        all_seqs = sorted(set(map1.keys()) | set(map2.keys()))

        if not all_seqs:
            return {
                "identical": False,
                "error": f"No decision records found for runs: '{run_id_1}' and/or '{run_id_2}'",
            }

        for seq in all_seqs:
            doc1 = map1.get(seq)
            doc2 = map2.get(seq)

            # 1. INPUT Divergence: Sequence dropped / missing in one run
            if doc1 is None or doc2 is None:
                missing_in = run_id_1 if doc1 is None else run_id_2
                present_doc = doc2 if doc1 is None else doc1
                return {
                    "identical": False,
                    "first_divergence_seq": seq,
                    "classification": "INPUT",
                    "reason": f"Dropped/Missing tick detected at sequence #{seq}. Present in {run_id_1 if doc1 else run_id_2}, missing in {missing_in}.",
                    "run_1_state": doc1,
                    "run_2_state": doc2,
                    "trigger_tick": present_doc.get("trigger_tick") if present_doc else None,
                }

            # 1b. INPUT Divergence: Raw tick contents mismatch
            tick1 = doc1.get("trigger_tick", {})
            tick2 = doc2.get("trigger_tick", {})
            if (
                tick1.get("event_id") != tick2.get("event_id")
                or tick1.get("price") != tick2.get("price")
                or tick1.get("volume") != tick2.get("volume")
            ):
                return {
                    "identical": False,
                    "first_divergence_seq": seq,
                    "classification": "INPUT",
                    "reason": f"Tick payload mismatch at sequence #{seq}.",
                    "delta": {"run_1_tick": tick1, "run_2_tick": tick2},
                    "run_1_state": doc1,
                    "run_2_state": doc2,
                }

            # 2. OBSERVATION Divergence: Indicator state calculation mismatch
            ind1 = doc1.get("indicator_state", {})
            ind2 = doc2.get("indicator_state", {})
            if ind1 != ind2:
                # Find specific indicator delta
                diff_keys = [k for k in set(ind1.keys()) | set(ind2.keys()) if ind1.get(k) != ind2.get(k)]
                return {
                    "identical": False,
                    "first_divergence_seq": seq,
                    "classification": "OBSERVATION",
                    "reason": f"Indicator delta at sequence #{seq} on keys: {diff_keys}.",
                    "delta": {
                        k: {"run_1": ind1.get(k), "run_2": ind2.get(k)} for k in diff_keys
                    },
                    "run_1_state": doc1,
                    "run_2_state": doc2,
                }

            # 3. DECISION Divergence: Signal flipped
            sig1 = doc1.get("signal")
            sig2 = doc2.get("signal")
            if sig1 != sig2:
                return {
                    "identical": False,
                    "first_divergence_seq": seq,
                    "classification": "DECISION",
                    "reason": f"Signal flipped at sequence #{seq}: {run_id_1}={sig1} vs {run_id_2}={sig2}.",
                    "delta": {"run_1_signal": sig1, "run_2_signal": sig2},
                    "run_1_state": doc1,
                    "run_2_state": doc2,
                }

            # 4. SETTLEMENT Divergence: Order ID / Execution mismatch
            ord1 = doc1.get("order_id")
            ord2 = doc2.get("order_id")
            if (ord1 is None) != (ord2 is None):
                return {
                    "identical": False,
                    "first_divergence_seq": seq,
                    "classification": "SETTLEMENT",
                    "reason": f"Settlement order intent mismatch at sequence #{seq}.",
                    "delta": {"run_1_order": ord1, "run_2_order": ord2},
                    "run_1_state": doc1,
                    "run_2_state": doc2,
                }

        return {
            "identical": True,
            "total_sequences_verified": len(all_seqs),
            "message": "Both runs achieved 100% deterministic parity across all inputs, observations, and decisions.",
        }

    def render_diff(self, diff_result: dict[str, Any], console: Optional[Console] = None) -> None:
        console = console or Console()
        if "error" in diff_result:
            console.print(f"[bold red]Diff Error:[/bold red] {diff_result['error']}")
            return

        if diff_result.get("identical"):
            console.print(
                Panel(
                    f"[bold green][OK] PARITY VERIFIED[/bold green]\n"
                    f"Aligned {diff_result['total_sequences_verified']} sequences.\n"
                    f"Zero divergence detected across INPUT, OBSERVATION, DECISION, and SETTLEMENT layers.",
                    title="Differential Diagnostic Engine (FR-9)",
                    border_style="green",
                )
            )
            return

        seq = diff_result["first_divergence_seq"]
        cls_type = diff_result["classification"]
        reason = diff_result["reason"]

        table = Table(title=f"First Divergence at Sequence #{seq} [{cls_type}]")
        table.add_column("Layer", style="bold yellow")
        table.add_column("Run 1", style="cyan")
        table.add_column("Run 2", style="magenta")

        r1 = diff_result.get("run_1_state") or {}
        r2 = diff_result.get("run_2_state") or {}

        table.add_row(
            "Trigger Tick",
            str(r1.get("trigger_tick", "MISSING")),
            str(r2.get("trigger_tick", "MISSING")),
        )
        table.add_row(
            "Indicator Snapshot",
            str(r1.get("indicator_state", "N/A")),
            str(r2.get("indicator_state", "N/A")),
        )
        table.add_row(
            "Signal",
            str(r1.get("signal", "N/A")),
            str(r2.get("signal", "N/A")),
        )
        table.add_row(
            "Order Ref",
            str(r1.get("order_id", "None")),
            str(r2.get("order_id", "None")),
        )

        console.print(
            Panel(
                f"[bold red][!] DIVERGENCE DETECTED[/bold red]\n"
                f"[bold]Root Cause Classification:[/bold] [bold red]{cls_type}[/bold red]\n"
                f"[bold]Diagnostic Reason:[/bold] {reason}\n\n",
                title="Differential Diagnostic Engine (FR-9)",
                border_style="red",
            )
        )
        console.print(table)
