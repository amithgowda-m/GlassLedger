from datetime import datetime, timezone
from decimal import Decimal
import sqlite3
from typing import Any, Optional

from glassledger.storage.postgres import PostgresStorage


def compute_xirr(
    cash_flows: list[tuple[datetime, float]],
    guess: float = 0.1,
    max_iterations: int = 100,
    tol: float = 1e-6,
) -> float:
    """
    Computes Extended Internal Rate of Return (XIRR) using Newton-Raphson
    with a robust bisection fallback.
    `cash_flows` is a list of (timestamp, amount) pairs where:
      - Outflows (investments/deposits) are negative.
      - Inflows (dividends/proceeds/terminal portfolio valuation) are positive.
    Returns annualized return as a percentage (e.g. 15.5 for 15.5%).
    """
    if not cash_flows or len(cash_flows) < 2:
        return 0.0

    # Ensure chronological order
    sorted_cfs = sorted(cash_flows, key=lambda x: x[0])
    d0 = sorted_cfs[0][0]

    # Check for valid cashflow signs (must have at least one negative and one positive)
    has_pos = any(c[1] > 0 for c in sorted_cfs)
    has_neg = any(c[1] < 0 for c in sorted_cfs)
    if not has_pos or not has_neg:
        return 0.0

    # Calculate year fractions
    items = []
    for d, amt in sorted_cfs:
        dt_days = max(0.0, (d - d0).total_seconds() / 86400.0)
        items.append((dt_days / 365.0, amt))

    max_t = items[-1][0]
    # If duration is very short (e.g. intraday / < 1 day), annualizing with exponent causes extreme distortion.
    # For very short durations, report the simple return % annualized or simple gain %.
    if max_t < (1.0 / 365.0):
        total_inflow = sum(amt for t, amt in items if amt > 0)
        total_outflow = abs(sum(amt for t, amt in items if amt < 0))
        if total_outflow > 0:
            return round(((total_inflow - total_outflow) / total_outflow) * 100.0, 2)
        return 0.0

    def npv(r: float) -> float:
        val = 0.0
        for t, amt in items:
            denom = (1.0 + r) ** t
            if abs(denom) < 1e-12:
                return float("inf")
            val += amt / denom
        return val

    def npv_prime(r: float) -> float:
        val = 0.0
        for t, amt in items:
            denom = (1.0 + r) ** (t + 1.0)
            if abs(denom) < 1e-12:
                continue
            val += -t * amt / denom
        return val

    # 1. Try Newton-Raphson
    r = guess
    for _ in range(max_iterations):
        f_val = npv(r)
        f_prime = npv_prime(r)
        if abs(f_prime) < 1e-9:
            break
        r_new = r - f_val / f_prime
        if abs(r_new - r) < tol:
            return round(r_new * 100.0, 2)
        if r_new <= -0.999 or r_new > 50.0:  # Out of reasonable bounds
            break
        r = r_new

    # 2. Bisection fallback within [-0.99, 10.0]
    low, high = -0.99, 10.0
    f_low, f_high = npv(low), npv(high)
    if f_low * f_high > 0:
        # If bracket doesn't cross zero, compute simple holding period return
        total_inflow = sum(amt for t, amt in items if amt > 0)
        total_outflow = abs(sum(amt for t, amt in items if amt < 0))
        if total_outflow > 0:
            simple_ret = ((total_inflow - total_outflow) / total_outflow) * 100.0
            return round(simple_ret, 2)
        return 0.0

    for _ in range(60):
        mid = (low + high) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < tol or (high - low) / 2.0 < tol:
            return round(mid * 100.0, 2)
        if f_low * f_mid < 0:
            high = mid
            f_high = f_mid
        else:
            low = mid
            f_low = f_mid

    return round(((low + high) / 2.0) * 100.0, 2)


class PortfolioManager:
    """
    Virtual Wallet & Portfolio Accounting Service.
    Computes:
      - Net Asset Value (NAV) Total & NAV per unit
      - Total units issued
      - Extended Internal Rate of Return (XIRR)
      - Individual asset weights, average prices, and mark-to-market valuations
    """

    INITIAL_VIRTUAL_CAPITAL = Decimal("100000.0000")
    BASE_UNITS = Decimal("10000.0000")

    def __init__(self, pg: Optional[PostgresStorage] = None):
        self.pg = pg or PostgresStorage()

    def get_portfolio_summary(self, investor_id: int = 1) -> dict[str, Any]:
        inv = self.pg.get_investor_account(investor_id)
        if not inv:
            wallet_balance = self.INITIAL_VIRTUAL_CAPITAL
            reserved_cash = Decimal("0.0000")
        else:
            wallet_balance = Decimal(str(inv.get("wallet_balance", self.INITIAL_VIRTUAL_CAPITAL)))
            reserved_cash = Decimal(str(inv.get("reserved_cash", "0.0000")))

        available_cash = wallet_balance - reserved_cash

        # Retrieve holdings
        raw_holdings = self.pg.get_holdings(investor_id)
        total_invested = Decimal("0.0000")
        total_securities_value = Decimal("0.0000")
        total_unrealized_pnl = Decimal("0.0000")

        holdings_list = []
        for h in raw_holdings:
            qty = int(h["quantity"])
            if qty <= 0:
                continue

            avg_price = Decimal(str(h["avg_buy_price"]))
            curr_price = Decimal(str(h.get("current_price") or avg_price))
            invested_val = (Decimal(qty) * avg_price).quantize(Decimal("0.0001"))
            market_val = (Decimal(qty) * curr_price).quantize(Decimal("0.0001"))
            unrealized = market_val - invested_val
            unrealized_pct = (
                ((curr_price - avg_price) / avg_price * Decimal("100.00")).quantize(Decimal("0.01"))
                if avg_price > 0
                else Decimal("0.00")
            )

            total_invested += invested_val
            total_securities_value += market_val
            total_unrealized_pnl += unrealized

            holdings_list.append({
                "symbol": h["symbol"],
                "units": qty,
                "avg_buy_price": f"{avg_price:.2f}",
                "current_price": f"{curr_price:.2f}",
                "invested_value": f"{invested_val:.2f}",
                "current_value": f"{market_val:.2f}",
                "unrealized_pnl": f"{unrealized:+.2f}",
                "unrealized_pnl_pct": f"{unrealized_pct:+.2f}%",
            })

        # Total Net Assets (Portfolio NAV Total)
        total_net_assets = wallet_balance + total_securities_value

        # Calculate Portfolio Weights
        for h in holdings_list:
            m_val = Decimal(h["current_value"])
            weight = (
                (m_val / total_net_assets * Decimal("100.00")).quantize(Decimal("0.01"))
                if total_net_assets > 0
                else Decimal("0.00")
            )
            h["weight_pct"] = f"{weight:.2f}%"

        # Unitized Fund Accounting
        total_units = self.BASE_UNITS
        nav_per_unit = (total_net_assets / total_units).quantize(Decimal("0.0001"))

        # Realized P&L from closed trades
        total_pnl = total_net_assets - self.INITIAL_VIRTUAL_CAPITAL
        total_pnl_pct = ((total_pnl / self.INITIAL_VIRTUAL_CAPITAL) * Decimal("100.00")).quantize(Decimal("0.02"))

        # 3. Build Cashflow History for XIRR
        cash_flows: list[tuple[datetime, float]] = []

        # Find earliest trade or default inception date
        with self.pg.get_connection_context() as conn:
            cur = conn.cursor()
            placeholder = "?" if isinstance(conn, sqlite3.Connection) else "%s"
            cur.execute(f"SELECT MIN(created_at) AS earliest FROM orders WHERE investor_id = {placeholder}", (investor_id,))
            earliest_row = cur.fetchone()
            # Access by column name: works for sqlite3.Row and psycopg dict rows alike.
            earliest_dt = earliest_row["earliest"] if earliest_row and earliest_row["earliest"] else None

            # Fetch all executed fills for cash flow timestamps
            cur.execute(
                """
                SELECT f.executed_at, f.quantity, f.price, f.fee, o.side 
                FROM fills f
                JOIN orders o ON f.order_id = o.order_id
                WHERE o.investor_id = ?
                ORDER BY f.executed_at ASC
                """
                if isinstance(conn, sqlite3.Connection)
                else """
                SELECT f.executed_at, f.quantity, f.price, f.fee, o.side 
                FROM fills f
                JOIN orders o ON f.order_id = o.order_id
                WHERE o.investor_id = %s
                ORDER BY f.executed_at ASC
                """,
                (investor_id,),
            )
            fills_rows = [dict(r) for r in cur.fetchall()]
            cur.close()

        # Inception cash flow (Initial Virtual Cash Deposit: -₹100,000)
        now_dt = datetime.now(timezone.utc)
        if earliest_dt:
            if isinstance(earliest_dt, str):
                try:
                    inception_dt = datetime.fromisoformat(earliest_dt.replace("Z", "+00:00"))
                except Exception:
                    inception_dt = now_dt
            else:
                inception_dt = earliest_dt
            if inception_dt.tzinfo is None:
                inception_dt = inception_dt.replace(tzinfo=timezone.utc)
        else:
            inception_dt = now_dt

        # Base cash flow: investor deposited initial capital
        cash_flows.append((inception_dt, -float(self.INITIAL_VIRTUAL_CAPITAL)))

        # Terminal cash flow: present value of total portfolio
        cash_flows.append((now_dt, float(total_net_assets)))

        xirr_val = compute_xirr(cash_flows)

        return {
            "portfolio": {
                "total_net_assets": f"{total_net_assets:.2f}",
                "nav_per_unit": f"{nav_per_unit:.4f}",
                "no_of_units": f"{total_units:.2f}",
                "xirr": f"{xirr_val:+.2f}%",
                "xirr_number": xirr_val,
                "initial_capital": f"{self.INITIAL_VIRTUAL_CAPITAL:.2f}",
                "total_pnl": f"{total_pnl:+.2f}",
                "total_pnl_pct": f"{total_pnl_pct:+.2f}%",
                "unrealized_pnl": f"{total_unrealized_pnl:+.2f}",
                "invested_capital": f"{total_invested:.2f}",
                "securities_market_value": f"{total_securities_value:.2f}",
                "wallet_balance": f"{wallet_balance:.2f}",
                "available_cash": f"{available_cash:.2f}",
                "reserved_cash": f"{reserved_cash:.2f}",
                "currency": "INR (₹)",
            },
            "holdings": holdings_list,
            "holdings_count": len(holdings_list),
            "recent_trades_count": len(fills_rows),
            "as_of": now_dt.isoformat(),
        }
