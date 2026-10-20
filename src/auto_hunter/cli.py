"""Command-line entry point (`auto-hunter`, defined in pyproject.toml).

Commands: collect, shortlist, report, history, market, reassess. Only `collect` touches
the network; the rest read the database. Run from the repo root, or pass --config-dir.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from auto_hunter import report
from auto_hunter.collectors import SautoCollector
from auto_hunter.config import load_config
from auto_hunter.db import Database
from auto_hunter.notify import send_alerts
from auto_hunter.pipeline import collect, reassess


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="auto-hunter")
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_collect = sub.add_parser("collect", help="Fetch listings and record changes")
    p_collect.add_argument("--profile", action="append", help="Only run these profiles")
    p_collect.add_argument("--no-alerts", action="store_true")

    p_short = sub.add_parser("shortlist", help="Active listings, most promising first")
    p_short.add_argument("--limit", type=int, default=25)
    p_short.add_argument("--profile")

    p_report = sub.add_parser("report", help="New, changed and removed listings")
    p_report.add_argument("--days", type=float, default=1)

    p_history = sub.add_parser("history", help="Price history and assessment of one listing")
    p_history.add_argument("source_id")

    sub.add_parser("market", help="Asking-price spread per generation and engine")
    sub.add_parser("reassess", help="Re-label stored listings after editing catalog/evaluation")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    # Windows consoles default to a legacy codepage that can't print Czech text.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    config = load_config(args.config_dir)
    db = Database(config.settings.database_path)
    try:
        if args.command == "collect":
            profiles = config.profiles
            if args.profile:
                profiles = [p for p in profiles if p.name in args.profile]
                if not profiles:
                    parser.error(f"No matching profiles: {args.profile}")
            collector = SautoCollector(config.settings.http, config.settings.collection)
            try:
                summary = collect(db, collector, config, profiles)
            finally:
                collector.close()
            logging.info("Run done: seen=%d new=%d changed=%d removed=%d errors=%d",
                         summary.seen, summary.new, summary.changed, summary.removed,
                         len(summary.errors))
            if not args.no_alerts:
                send_alerts(summary.alerts)
            return 1 if summary.errors and not summary.seen else 0
        if args.command == "shortlist":
            print(report.shortlist_report(db, config, args.limit, args.profile))
        elif args.command == "report":
            print(report.changes_report(db, args.days))
        elif args.command == "history":
            print(report.history_report(db, args.source_id))
        elif args.command == "market":
            print(report.market_report(db, config))
        elif args.command == "reassess":
            print(f"Re-assessed {reassess(db, config)} listings.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
