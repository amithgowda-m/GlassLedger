"""
Convert local candle files (your own data, not included in this repository) into GlassLedger
15-minute tick CSVs.

Expected layout (read-only): data/candles/candles_<tf>/symbol=<SYM>/period=<P>/part.parquet with
the generic candle schema: symbol, ts_utc_ns (bar START, epoch ns UTC), trade_date (IST session
date), open/high/low/close (DOUBLE), volume, oi.

Output tick semantics:
  * one tick per 15-minute bar, price = bar close, volume = bar volume;
  * timestamp = bar END (bar start + 15 min) in UTC, so a strategy never sees an incomplete bar;
  * prices are exact decimal strings (4 dp), never floats;
  * only regular-session bars (bar start 09:15..15:15 IST), sorted, no duplicate timestamps.

15m bars are rebuilt from 1m candles: 09:15 IST lies on the UTC 15-minute grid
(IST = UTC+5:30 = 22 x 15 min), so bucket_start = floor(ts / 15min) * 15min.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import random
from typing import Any, Optional

MIN_NS = 60_000_000_000
BUCKET_NS = 15 * MIN_NS
IST_OFFSET_MIN = 330
SESSION_FIRST_1M = 9 * 60 + 15       # 09:15 IST, first 1m bar start
SESSION_LAST_1M = 15 * 60 + 29       # 15:29 IST, last 1m bar start
SESSION_LAST_15M = 15 * 60 + 15      # 15:15 IST, last 15m bar start
BARS_PER_FULL_SESSION = 25
PRICE_Q = Decimal("0.0001")


class ConversionError(Exception):
    pass


@dataclass
class ReportContext:
    """What the external quality report says about a symbol (all optional)."""
    special_sessions: set[str] = field(default_factory=set)
    bad_print_days: set[str] = field(default_factory=set)
    masked_days: set[str] = field(default_factory=set)
    corporate_actions: list[dict[str, Any]] = field(default_factory=list)   # confirmed / candidate splits & bonuses
    source_file: Optional[str] = None


def ist_minute_of_day(ts_utc_ns: int) -> int:
    return (ts_utc_ns // MIN_NS + IST_OFFSET_MIN) % 1440


def bucket_start_ns(ts_utc_ns: int) -> int:
    return (ts_utc_ns // BUCKET_NS) * BUCKET_NS


def to_price_str(x: float) -> str:
    """Exact 4-dp decimal string from a vendor float that is really a 2-dp price."""
    d = Decimal(repr(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if abs(Decimal(repr(x)) - d) > Decimal("0.000001"):
        raise ConversionError(f"price {x!r} is not a 2-decimal price")
    return str(d.quantize(PRICE_Q))


def iso_utc(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).isoformat()


# --------------------------------------------------------------------------- report

def load_report(report_path: Path | str | None, symbol: str) -> ReportContext:
    """Extracts exclusions for `symbol` from a quality_*.json report (read-only)."""
    if not report_path:
        return ReportContext()
    data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    ctx = ReportContext(source_file=str(report_path))
    ctx.special_sessions = set(data.get("special_sessions_excluded") or [])
    ctx.bad_print_days = {e["date"] for e in (data.get("bad_1m_print_exceptions") or []) if "date" in e}
    for m in data.get("masked_symbol_days") or []:
        if isinstance(m, dict) and m.get("symbol") == symbol and m.get("trade_date"):
            ctx.masked_days.add(m["trade_date"])
    by_date: dict[str, dict[str, Any]] = {}
    for key, default_status in (("corporate_actions", "confirmed"), ("detected_actions", None)):
        for a in data.get(key) or []:
            if not (isinstance(a, dict) and a.get("symbol") == symbol and a.get("ex_date")):
                continue
            entry = {
                "ex_date": a["ex_date"],
                "kind": a.get("kind") or a.get("label"),
                "status": a.get("status") or default_status,
                "source": key,
            }
            prev = by_date.get(a["ex_date"])
            # One action per ex-date; a confirmed record outranks a price-only candidate.
            if prev is None or (prev["status"] != "confirmed" and entry["status"] == "confirmed"):
                by_date[a["ex_date"]] = entry
    ctx.corporate_actions = [by_date[d] for d in sorted(by_date)]
    return ctx


def latest_report(reports_dir: Path | str) -> Optional[Path]:
    files = sorted(p for p in Path(reports_dir).glob("quality_*.json") if "_subset" not in p.name)
    return files[-1] if files else None


# --------------------------------------------------------------------------- core aggregation

def aggregate_15m(rows_1m: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Rebuild 15m bars from 1m bars (each row: ts_utc_ns, trade_date, open, high, low, close, volume).
    Only regular-session 1m bars (09:15..15:29 IST) are used. open = first 1m open,
    close = last 1m close, high/low = extremes, volume = sum, n_1m = bars in the bucket.
    """
    buckets: dict[int, dict[str, Any]] = {}
    for r in sorted(rows_1m, key=lambda r: r["ts_utc_ns"]):
        m = ist_minute_of_day(r["ts_utc_ns"])
        if not (SESSION_FIRST_1M <= m <= SESSION_LAST_1M):
            continue
        b = bucket_start_ns(r["ts_utc_ns"])
        bar = buckets.get(b)
        if bar is None:
            buckets[b] = {
                "bucket_start_ns": b, "trade_date": r["trade_date"], "open": r["open"], "high": r["high"],
                "low": r["low"], "close": r["close"], "volume": int(r["volume"]), "n_1m": 1,
            }
        else:
            bar["high"] = max(bar["high"], r["high"])
            bar["low"] = min(bar["low"], r["low"])
            bar["close"] = r["close"]
            bar["volume"] += int(r["volume"])
            bar["n_1m"] += 1
    return [buckets[k] for k in sorted(buckets)]


def bars_to_ticks(symbol: str, bars: list[dict[str, Any]]) -> list[dict[str, str]]:
    """One tick per bar, stamped at bar END (start + 15 min) in UTC; prices as decimal strings."""
    ticks, seen = [], set()
    for bar in sorted(bars, key=lambda b: b["bucket_start_ns"]):
        start_min = ist_minute_of_day(bar["bucket_start_ns"])
        if not (SESSION_FIRST_1M <= start_min <= SESSION_LAST_15M):
            raise ConversionError(f"bar outside regular session at {iso_utc(bar['bucket_start_ns'])}")
        end_ns = bar["bucket_start_ns"] + BUCKET_NS
        if end_ns in seen:
            raise ConversionError(f"duplicate tick timestamp {iso_utc(end_ns)}")
        seen.add(end_ns)
        end = datetime.fromtimestamp(end_ns / 1e9, tz=timezone.utc)
        ticks.append({
            "event_id": f"cdl_{symbol}_15m_{end:%Y%m%dT%H%MZ}",
            "symbol": symbol,
            "price": to_price_str(bar["close"]),
            "volume": str(int(bar["volume"])),
            "timestamp": end.isoformat(),
        })
    return ticks


# --------------------------------------------------------------------------- DuckDB I/O

def _src(data_dir: Path, tf: str, symbol: str) -> str:
    glob = (Path(data_dir) / f"candles_{tf}" / f"symbol={symbol}" / "*" / "*.parquet").as_posix()
    return f"read_parquet('{glob}', hive_partitioning=true)"


def read_candles(data_dir: Path | str, tf: str, symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    import duckdb

    sym_dir = Path(data_dir) / f"candles_{tf}" / f"symbol={symbol}"
    if not sym_dir.exists():
        return []
    con = duckdb.connect()
    try:
        cur = con.execute(
            f"SELECT ts_utc_ns, trade_date, open, high, low, close, volume FROM {_src(Path(data_dir), tf, symbol)} "
            "WHERE trade_date BETWEEN ? AND ? ORDER BY ts_utc_ns",
            [start, end],
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        con.close()


# --------------------------------------------------------------------------- verification

def compare_15m(rebuilt: list[dict[str, Any]], vendor: list[dict[str, Any]]) -> dict[str, Any]:
    """Field-by-field comparison of rebuilt vs provided 15m bars keyed by bar start."""
    v = {r["ts_utc_ns"]: r for r in vendor
         if SESSION_FIRST_1M <= ist_minute_of_day(r["ts_utc_ns"]) <= SESSION_LAST_15M}
    b = {r["bucket_start_ns"]: r for r in rebuilt}
    out = {"bars_compared": 0, "only_in_rebuild": sorted(set(b) - set(v)), "only_in_vendor": sorted(set(v) - set(b)),
           "mismatch": {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 0}, "examples": []}
    for ts in sorted(set(b) & set(v)):
        out["bars_compared"] += 1
        for f in ("open", "high", "low", "close", "volume"):
            same = (int(b[ts][f]) == int(v[ts][f])) if f == "volume" else (to_price_str(b[ts][f]) == to_price_str(v[ts][f]))
            if not same:
                out["mismatch"][f] += 1
                if len(out["examples"]) < 10:
                    out["examples"].append({"bar_start": iso_utc(ts), "field": f, "rebuilt": b[ts][f], "vendor": v[ts][f]})
    return out


def verify_15m_against_1m(data_dir: Path | str, symbol: str, n_days: int = 20, seed: int = 7,
                          exclude_days: set[str] | None = None) -> dict[str, Any]:
    """Rebuild 15m from 1m on n random regular trading days that have provided 15m bars, and compare."""
    import duckdb

    con = duckdb.connect()
    try:
        days = [r[0] for r in con.execute(
            f"SELECT DISTINCT trade_date FROM {_src(Path(data_dir), '15m', symbol)} ORDER BY 1").fetchall()]
    finally:
        con.close()
    days = [d for d in days if d not in (exclude_days or set())]
    picked = sorted(random.Random(seed).sample(days, min(n_days, len(days))))
    per_day, totals = [], {"bars_compared": 0, "only_in_rebuild": 0, "only_in_vendor": 0,
                           "mismatch": {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 0}}
    examples = []
    for d in picked:
        res = compare_15m(aggregate_15m(read_candles(data_dir, "1m", symbol, d, d)),
                          read_candles(data_dir, "15m", symbol, d, d))
        totals["bars_compared"] += res["bars_compared"]
        totals["only_in_rebuild"] += len(res["only_in_rebuild"])
        totals["only_in_vendor"] += len(res["only_in_vendor"])
        for f, n in res["mismatch"].items():
            totals["mismatch"][f] += n
        examples.extend(res["examples"][: max(0, 10 - len(examples))])
        per_day.append({"trade_date": d, "bars": res["bars_compared"],
                        "mismatches": sum(res["mismatch"].values()),
                        "only_in_rebuild": len(res["only_in_rebuild"]), "only_in_vendor": len(res["only_in_vendor"])})
    return {"symbol": symbol, "days": picked, "totals": totals, "per_day": per_day, "examples": examples}


# --------------------------------------------------------------------------- conversion

def plan_range(start: str, end: str, ctx: ReportContext, split_policy: str = "fail") -> tuple[str, str, list[dict]]:
    """
    Returns (start, end, actions_in_range). Raw prices jump at a split/bonus ex-date, so a range that
    contains one is refused ('fail'), or cut to the part before the first ex-date ('before') or from the
    last ex-date on ('after').
    """
    inside = sorted((a for a in ctx.corporate_actions if start <= a["ex_date"] <= end), key=lambda a: a["ex_date"])
    if not inside:
        return start, end, []
    if split_policy == "fail":
        listed = ", ".join(f"{a['ex_date']} {a['kind']} ({a['status']})" for a in inside)
        raise ConversionError(f"range {start}..{end} contains split/bonus ex-date(s): {listed}; "
                              "use split_policy='before' or 'after', or choose another range")
    if split_policy == "before":
        cut = inside[0]["ex_date"]
        prev = datetime.strptime(cut, "%Y-%m-%d").toordinal() - 1
        return start, datetime.fromordinal(prev).strftime("%Y-%m-%d"), inside
    if split_policy == "after":
        return inside[-1]["ex_date"], end, inside
    raise ValueError(f"unknown split_policy {split_policy!r}")


def convert(data_dir: Path | str, symbol: str, start: str, end: str, ctx: ReportContext | None = None,
            split_policy: str = "fail") -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Returns (ticks, report). Raises ConversionError on unsafe input."""
    ctx = ctx or ReportContext()
    eff_start, eff_end, actions = plan_range(start, end, ctx, split_policy)
    rows = read_candles(data_dir, "1m", symbol, eff_start, eff_end)
    if not rows:
        raise ConversionError(f"no 1m candles for {symbol} in {eff_start}..{eff_end}")

    excluded_days: dict[str, str] = {}
    for d in sorted({r["trade_date"] for r in rows}):
        if d in ctx.special_sessions:
            excluded_days[d] = "special session listed in report"
        elif d in ctx.bad_print_days:
            excluded_days[d] = "vendor bad-print day listed in report"
        elif d in ctx.masked_days:
            excluded_days[d] = "masked symbol-day listed in report"
    kept = [r for r in rows if r["trade_date"] not in excluded_days]
    outside_session = sum(1 for r in kept if not (SESSION_FIRST_1M <= ist_minute_of_day(r["ts_utc_ns"]) <= SESSION_LAST_1M))

    bars = aggregate_15m(kept)
    ticks = bars_to_ticks(symbol, bars)

    per_day: dict[str, int] = {}
    for b in bars:
        per_day[b["trade_date"]] = per_day.get(b["trade_date"], 0) + 1
    partial_bars = [{"bar_start": iso_utc(b["bucket_start_ns"]), "n_1m": b["n_1m"]} for b in bars if b["n_1m"] < 15]
    short_sessions = {d: n for d, n in per_day.items() if n < BARS_PER_FULL_SESSION}
    zero_volume_bars = [iso_utc(b["bucket_start_ns"]) for b in bars if b["volume"] == 0]

    daily = read_candles(data_dir, "1d", symbol, eff_start, eff_end)
    daily_days = {r["trade_date"] for r in daily}
    missing_vs_daily = sorted(daily_days - set(per_day) - set(excluded_days))

    report = {
        "symbol": symbol,
        "requested_range": [start, end],
        "effective_range": [eff_start, eff_end],
        "split_policy": split_policy,
        "corporate_actions_in_range": actions,
        "corporate_actions_for_symbol": ctx.corporate_actions,
        "report_source": ctx.source_file,
        "source_1m_rows": len(rows),
        "excluded_days": excluded_days,
        "excluded_1m_rows": len(rows) - len(kept),
        "outside_session_1m_rows_dropped": outside_session,
        "sessions": len(per_day),
        "ticks": len(ticks),
        "first_tick": ticks[0] if ticks else None,
        "last_tick": ticks[-1] if ticks else None,
        "short_sessions": short_sessions,
        "partial_bars": partial_bars,
        "zero_volume_bars": zero_volume_bars,
        "days_in_1d_missing_from_1m": missing_vs_daily,
    }
    return ticks, report


def write_csv(ticks: list[dict[str, str]], path: Path | str) -> None:
    import csv

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["event_id", "symbol", "price", "volume", "timestamp"])
        w.writeheader()
        w.writerows(ticks)
