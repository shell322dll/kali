#!/usr/bin/env python3
"""Update site/data/flights.json from the public SVO board."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from flight_data import FLIGHTS, MOSCOW, SourceError, fetch_flight, read_dataset, update_dataset, write_dataset


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "site" / "data" / "flights.json",
    )
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        help="Base Moscow date in YYYY-MM-DD format (defaults to today)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=2,
        help="Number of calendar days to request, ending on the base date (default: 2)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.days <= 30:
        print("--days must be between 1 and 30", file=sys.stderr)
        return 2
    base_day = args.date or datetime.now(MOSCOW).date()
    requested_days = tuple(
        base_day - timedelta(days=offset) for offset in range(args.days - 1, -1, -1)
    )
    records = []
    successful_requests = 0
    errors = []

    for day in requested_days:
        for flight_code in FLIGHTS:
            try:
                record = fetch_flight(flight_code, day)
                successful_requests += 1
                if record:
                    records.append(record)
                    print(f"Found {flight_code} for {day}")
                else:
                    print(f"No {flight_code} record for {day}")
            except SourceError as exc:
                errors.append(str(exc))
                print(str(exc), file=sys.stderr)

    if successful_requests == 0:
        print("All SVO requests failed; existing data was not changed.", file=sys.stderr)
        return 1

    dataset = read_dataset(args.data)
    updated, changed = update_dataset(dataset, records)
    if changed:
        write_dataset(args.data, updated)
        print(f"Updated {args.data}")
    else:
        print("No flight data changes")

    if errors:
        print(f"Completed with {len(errors)} source error(s); successful data was preserved.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
