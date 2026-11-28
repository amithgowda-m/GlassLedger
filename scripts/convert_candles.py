"""
Convert local candle files (data/candles, read-only; your own data, not included in this
repository) into a GlassLedger 15-minute tick CSV.

Examples:
  python scripts/convert_candles.py convert RELIANCE 2025-01-01 2025-12-31 --dataset reliance_2025_15m
  python scripts/convert_candles.py verify SBIN --days 20

Outputs go to data/imports/ (git-ignored): <dataset>.csv and <dataset>.report.json.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from glassledger.ingest.candles import (
    ConversionError, convert, latest_report, load_report, verify_15m_against_1m, write_csv,
)

DATA_DIR = Path("data/candles")
REPORTS_DIR = Path("data/reports")
OUT_DIR = Path("data/imports")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("convert", help="build a 15m tick CSV from 1m candles")
    c.add_argument("symbol")
    c.add_argument("start", help="first trade date, YYYY-MM-DD")
    c.add_argument("end", help="last trade date, YYYY-MM-DD")
    c.add_argument("--dataset", required=True, help="dataset name; also the output file stem")
    c.add_argument("--report", help="quality_*.json to read exclusions from (default: latest in data/reports)")
    c.add_argument("--split-policy", choices=["fail", "before", "after"], default="fail")

    v = sub.add_parser("verify", help="compare provided 15m bars with bars rebuilt from 1m")
    v.add_argument("symbol")
    v.add_argument("--days", type=int, default=20)
    v.add_argument("--seed", type=int, default=7)
    v.add_argument("--report", help="quality_*.json (special sessions are skipped)")

    args = p.parse_args(argv)
    report_path = args.report or latest_report(REPORTS_DIR)

    if args.cmd == "verify":
        ctx = load_report(report_path, args.symbol)
        res = verify_15m_against_1m(DATA_DIR, args.symbol, args.days, args.seed,
                                    exclude_days=ctx.special_sessions | ctx.bad_print_days)
        print(json.dumps(res, indent=2, default=str))
        return 0

    ctx = load_report(report_path, args.symbol)
    try:
        ticks, report = convert(DATA_DIR, args.symbol, args.start, args.end, ctx, args.split_policy)
    except ConversionError as e:
        print(f"CONVERSION REFUSED: {e}", file=sys.stderr)
        return 2
    out_csv = OUT_DIR / f"{args.dataset}.csv"
    write_csv(ticks, out_csv)
    report["dataset"] = args.dataset
    report["output_csv"] = out_csv.as_posix()
    (OUT_DIR / f"{args.dataset}.report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("symbol", "effective_range", "sessions", "ticks", "excluded_days",
                                              "short_sessions", "days_in_1d_missing_from_1m")}, indent=2))
    print(f"wrote {out_csv} and {OUT_DIR / (args.dataset + '.report.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
