"""
Candle -> GlassLedger converter: bar alignment, end-of-bar stamping, IST->UTC, exact decimal prices,
split/bonus exclusion, no duplicates. Uses tiny synthetic Parquet fixtures in a temp dir
(same layout as data/candles), never real data.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import re

import pytest

duckdb = pytest.importorskip("duckdb")

from glassledger.domain.models import Tick  # noqa: E402
from glassledger.engine.sequencer import Sequencer  # noqa: E402
from glassledger.ingest.candles import (  # noqa: E402
    ConversionError, ReportContext, aggregate_15m, bars_to_ticks, bucket_start_ns, convert, load_report,
    to_price_str, write_csv,
)

IST = timezone(timedelta(hours=5, minutes=30))
MIN = 60_000_000_000


def ns(dt: datetime) -> int:
    return int(dt.timestamp()) * 1_000_000_000


def session_1m(day: str, base: float = 1000.0, start="09:15", end="15:29", extra: list[str] = ()) -> list[dict]:
    """One regular session of 1m bars (bar START stamps), prices rising 0.05 per minute."""
    d = datetime.strptime(day, "%Y-%m-%d")
    t = datetime.combine(d, datetime.strptime(start, "%H:%M").time(), IST)
    stop = datetime.combine(d, datetime.strptime(end, "%H:%M").time(), IST)
    rows, i = [], 0
    while t <= stop:
        p = round(base + 0.05 * i, 2)
        rows.append({"ts_utc_ns": ns(t), "trade_date": day, "open": p, "high": round(p + 0.10, 2),
                     "low": round(p - 0.10, 2), "close": round(p + 0.05, 2), "volume": 100 + i})
        t += timedelta(minutes=1)
        i += 1
    for hhmm in extra:  # e.g. a Muhurat-style bar outside the regular session
        tt = datetime.combine(d, datetime.strptime(hhmm, "%H:%M").time(), IST)
        rows.append({"ts_utc_ns": ns(tt), "trade_date": day, "open": base, "high": base, "low": base,
                     "close": base, "volume": 1})
    return rows


def write_fixture(root, symbol: str, rows: list[dict], tf: str = "1m") -> None:
    by_period: dict[str, list[dict]] = {}
    for r in rows:
        by_period.setdefault(r["trade_date"][:7], []).append(r)
    for period, part in by_period.items():
        out = root / f"candles_{tf}" / f"symbol={symbol}" / f"period={period}"
        out.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect()
        con.execute("CREATE TABLE t (symbol VARCHAR, ts_utc_ns BIGINT, trade_date VARCHAR, open DOUBLE, high DOUBLE,"
                    " low DOUBLE, close DOUBLE, volume BIGINT, oi BIGINT)")
        con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                        [(symbol, r["ts_utc_ns"], r["trade_date"], r["open"], r["high"], r["low"], r["close"], r["volume"])
                         for r in part])
        con.execute(f"COPY t TO '{(out / 'part.parquet').as_posix()}' (FORMAT PARQUET)")
        con.close()


# ---------------------------------------------------------------- alignment and aggregation

def test_buckets_align_to_0915_ist_and_aggregate_ohlcv():
    rows = session_1m("2025-01-02")
    bars = aggregate_15m(rows)
    assert len(bars) == 25
    starts_ist = [datetime.fromtimestamp(b["bucket_start_ns"] / 1e9, IST).strftime("%H:%M") for b in bars]
    assert starts_ist[0] == "09:15" and starts_ist[1] == "09:30" and starts_ist[-1] == "15:15"
    first = rows[:15]
    b0 = bars[0]
    assert b0["open"] == first[0]["open"]
    assert b0["close"] == first[-1]["close"]                  # close = last 1m close in the bucket
    assert b0["high"] == max(r["high"] for r in first)
    assert b0["low"] == min(r["low"] for r in first)
    assert b0["volume"] == sum(r["volume"] for r in first)    # volume = sum of 1m volumes
    assert all(b["n_1m"] == 15 for b in bars)


def test_bucket_start_is_floor_on_the_15_minute_grid():
    t = ns(datetime(2025, 1, 2, 9, 29, tzinfo=IST))
    assert bucket_start_ns(t) == ns(datetime(2025, 1, 2, 9, 15, tzinfo=IST))
    assert bucket_start_ns(t + MIN) == ns(datetime(2025, 1, 2, 9, 30, tzinfo=IST))


def test_bars_outside_regular_session_are_dropped():
    rows = session_1m("2025-01-02", extra=["09:00", "15:30", "18:15"])
    assert len(aggregate_15m(rows)) == 25


# ---------------------------------------------------------------- stamping, timezone, prices

def test_ticks_are_stamped_at_bar_end_in_utc():
    ticks = bars_to_ticks("TEST", aggregate_15m(session_1m("2025-01-02")))
    first, last = ticks[0], ticks[-1]
    assert first["timestamp"] == "2025-01-02T04:00:00+00:00"     # 09:15 IST bar ends 09:30 IST = 04:00 UTC
    assert last["timestamp"] == "2025-01-02T10:00:00+00:00"      # 15:15 IST bar ends 15:30 IST = 10:00 UTC
    for t in ticks:
        dt = datetime.fromisoformat(t["timestamp"])
        assert dt.utcoffset() == timedelta(0)
        assert dt.astimezone(IST).strftime("%H:%M") >= "09:30"
        assert dt.astimezone(IST).strftime("%H:%M") <= "15:30"


def test_prices_are_exact_decimal_strings_never_floats():
    ticks = bars_to_ticks("TEST", aggregate_15m(session_1m("2025-01-02", base=1216.3)))
    for t in ticks:
        assert isinstance(t["price"], str) and re.fullmatch(r"\d+\.\d{4}", t["price"])
    assert to_price_str(0.1 + 0.2) == "0.3000"          # binary float noise removed exactly
    assert to_price_str(1132.3) == "1132.3000"
    with pytest.raises(ConversionError):
        to_price_str(1.23456)                            # not a 2-dp price: refuse, don't guess


def test_csv_round_trips_through_glassledger_sequencer(tmp_path):
    ticks = bars_to_ticks("TEST", aggregate_15m(session_1m("2025-01-02")))
    write_csv(ticks, tmp_path / "x.csv")
    loaded = Sequencer.load_csv(tmp_path / "x.csv")
    assert len(loaded) == 25 and all(isinstance(t, Tick) and isinstance(t.price, Decimal) for t in loaded)
    assert loaded[0].timestamp == datetime(2025, 1, 2, 4, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- duplicates

def test_duplicate_1m_rows_do_not_create_duplicate_ticks(tmp_path):
    rows = session_1m("2025-01-02")
    write_fixture(tmp_path, "DUP", rows + rows[:30])        # first 30 minutes stored twice
    ticks, report = convert(tmp_path, "DUP", "2025-01-02", "2025-01-02")
    stamps = [t["timestamp"] for t in ticks]
    assert len(stamps) == len(set(stamps)) == 25
    assert len({t["event_id"] for t in ticks}) == 25


def test_duplicate_bucket_is_refused():
    bars = aggregate_15m(session_1m("2025-01-02"))
    with pytest.raises(ConversionError, match="duplicate"):
        bars_to_ticks("TEST", bars + [dict(bars[0])])


# ---------------------------------------------------------------- end-to-end, exclusions and splits

def test_convert_end_to_end_with_special_session_excluded(tmp_path):
    rows = session_1m("2025-01-02") + session_1m("2025-01-03") + session_1m("2025-01-04", start="13:45", end="14:44")
    write_fixture(tmp_path, "E2E", rows)
    ctx = ReportContext(special_sessions={"2025-01-04"})
    ticks, report = convert(tmp_path, "E2E", "2025-01-01", "2025-01-31", ctx)
    assert len(ticks) == 50 and report["sessions"] == 2
    assert report["excluded_days"] == {"2025-01-04": "special session listed in report"}
    assert [t["timestamp"] for t in ticks] == sorted(t["timestamp"] for t in ticks)


def test_split_inside_range_is_refused_by_default(tmp_path):
    write_fixture(tmp_path, "SPL", session_1m("2025-01-02") + session_1m("2025-01-03", base=500.0))
    ctx = ReportContext(corporate_actions=[{"ex_date": "2025-01-03", "kind": "split", "status": "confirmed", "source": "x"}])
    with pytest.raises(ConversionError, match="2025-01-03 split"):
        convert(tmp_path, "SPL", "2025-01-01", "2025-01-31", ctx)


def test_split_policy_before_and_after_cut_at_the_ex_date(tmp_path):
    write_fixture(tmp_path, "SPL", session_1m("2025-01-02") + session_1m("2025-01-03", base=500.0))
    ctx = ReportContext(corporate_actions=[{"ex_date": "2025-01-03", "kind": "split", "status": "confirmed", "source": "x"}])
    before, rep_b = convert(tmp_path, "SPL", "2025-01-01", "2025-01-31", ctx, split_policy="before")
    after, rep_a = convert(tmp_path, "SPL", "2025-01-01", "2025-01-31", ctx, split_policy="after")
    assert rep_b["effective_range"] == ["2025-01-01", "2025-01-02"] and len(before) == 25
    assert rep_a["effective_range"] == ["2025-01-03", "2025-01-31"] and len(after) == 25
    assert Decimal(before[-1]["price"]) > 1000 and Decimal(after[0]["price"]) < 600   # no cross-split series
    assert rep_b["corporate_actions_in_range"][0]["ex_date"] == "2025-01-03"


def test_report_loader_merges_confirmed_and_candidate_actions(tmp_path):
    report = {
        "special_sessions_excluded": ["2025-02-01"],
        "bad_1m_print_exceptions": [{"date": "2024-06-25", "reason": "x"}],
        "masked_symbol_days": [{"symbol": "ABC", "trade_date": "2025-04-07", "reason": "x"}],
        "corporate_actions": [{"symbol": "ABC", "ex_date": "2025-04-07", "kind": "split"}],
        "detected_actions": [
            {"symbol": "ABC", "ex_date": "2025-04-07", "label": "split 1:2", "status": "confirmed"},
            {"symbol": "ABC", "ex_date": "2025-09-01", "label": "bonus 1:4", "status": "price-only"},
            {"symbol": "XYZ", "ex_date": "2025-05-05", "label": "bonus 1:1", "status": "confirmed"},
        ],
    }
    p = tmp_path / "quality_test.json"
    p.write_text(json.dumps(report), encoding="utf-8")
    ctx = load_report(p, "ABC")
    assert ctx.special_sessions == {"2025-02-01"} and ctx.bad_print_days == {"2024-06-25"}
    assert ctx.masked_days == {"2025-04-07"}
    assert [(a["ex_date"], a["status"]) for a in ctx.corporate_actions] == [("2025-04-07", "confirmed"), ("2025-09-01", "price-only")]


def test_missing_symbol_is_refused(tmp_path):
    with pytest.raises(ConversionError, match="no 1m candles"):
        convert(tmp_path, "NOPE", "2025-01-01", "2025-01-31")


def test_event_ids_use_neutral_prefix():
    ticks = bars_to_ticks("TEST", aggregate_15m(session_1m("2025-01-02")))
    assert ticks[0]["event_id"] == "cdl_TEST_15m_20250102T0400Z"
