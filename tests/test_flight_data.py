from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flight_data import (  # noqa: E402
    SourceError,
    build_detail_url,
    compute_statistics,
    fetch_flight,
    normalize_svo_item,
    read_dataset,
    update_dataset,
    write_dataset,
)


def svo_item(flight: str = "1032", **overrides):
    item = {
        "i_id": "9582556" if flight == "1032" else "9582517",
        "co": {"code": "SU"},
        "flt": flight,
        "dat": "2026-06-28T00:00:00+03:00",
        "status_id": "220",
        "vip_status_rus": "Совершил посадку",
    }
    if flight == "1032":
        item.update(
            {
                "t_st": "2026-06-28T07:25:00+03:00",
                "t_at": "2026-06-28T07:35:00+03:00",
                "t_st_mar": "2026-06-28T10:05:00+03:00",
                "t_at_mar": "2026-06-28T09:52:00+03:00",
            }
        )
    else:
        item.update(
            {
                "t_st_mar": "2026-06-28T04:00:00+03:00",
                "t_at_mar": "2026-06-28T04:12:00+03:00",
                "t_st": "2026-06-28T06:30:00+03:00",
                "t_at": "2026-06-28T06:15:00+03:00",
            }
        )
    item.update(overrides)
    return item


class FakeResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class SequenceOpener:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.urls = []

    def __call__(self, request, **_kwargs):
        self.urls.append(request.full_url)
        return FakeResponse(self.payloads.pop(0))


class FlightDataTests(unittest.TestCase):
    def test_normalizes_su1032_to_utc(self):
        result = normalize_svo_item("SU1032", svo_item())
        record = result["record"]
        self.assertEqual(result["date"], "2026-06-28")
        self.assertEqual(record["actualDeparture"], "2026-06-28T04:35:00Z")
        self.assertEqual(record["actualArrival"], "2026-06-28T06:52:00Z")
        self.assertEqual(record["durationMinutes"], 137)
        self.assertEqual(record["departureDelayMinutes"], 10)
        self.assertEqual(record["arrivalDelayMinutes"], -13)
        self.assertEqual(record["sourceFlightId"], "9582556")
        self.assertEqual(
            record["sourceUrl"],
            "https://www.svo.aero/ru/timetable/departure/flight/9582556/info",
        )

    def test_normalizes_su1009_origin_fields(self):
        result = normalize_svo_item("SU1009", svo_item("1009"))
        record = result["record"]
        self.assertEqual(record["actualDeparture"], "2026-06-28T01:12:00Z")
        self.assertEqual(record["actualArrival"], "2026-06-28T03:15:00Z")
        self.assertEqual(record["durationMinutes"], 123)
        self.assertEqual(record["departureDelayMinutes"], 12)
        self.assertIn("/timetable/arrival/flight/9582517/info", record["sourceUrl"])

    def test_fetches_official_svo_detail_after_board_lookup(self):
        board_item = svo_item(t_at=None, t_at_mar=None)
        detail_item = svo_item()
        opener = SequenceOpener(
            json.dumps({"items": [board_item]}).encode(),
            json.dumps(detail_item).encode(),
        )

        result = fetch_flight("SU1032", date(2026, 6, 28), opener=opener)

        self.assertEqual(result["record"]["actualDeparture"], "2026-06-28T04:35:00Z")
        self.assertEqual(len(opener.urls), 2)
        self.assertEqual(opener.urls[1], build_detail_url("9582556"))

    def test_cancellation_is_explicit(self):
        item = svo_item(status_id="230", vip_status_rus="Отменен", t_at=None, t_at_mar=None)
        record = normalize_svo_item("SU1032", item)["record"]
        self.assertEqual(record["status"], "cancelled")
        self.assertIsNone(record["actualDeparture"])

    def test_diversion_is_critical_but_not_cancelled(self):
        item = svo_item(vip_status_rus="Перенаправлен на запасной аэродром", t_at_mar=None)
        record = normalize_svo_item("SU1032", item)["record"]
        self.assertEqual(record["status"], "diverted")

    def test_missing_result_is_not_a_cancellation(self):
        payload = json.dumps({"items": [], "pagination": {}}).encode()

        def opener(*_args, **_kwargs):
            return FakeResponse(payload)

        self.assertIsNone(fetch_flight("SU1032", date(2026, 6, 28), opener=opener))

    def test_source_failure_raises_without_dataset_write(self):
        def opener(*_args, **_kwargs):
            raise OSError("offline")

        with self.assertRaises(SourceError):
            fetch_flight("SU1032", date(2026, 6, 28), opener=opener)

    def test_detail_failure_raises_instead_of_using_incomplete_board_item(self):
        opener = SequenceOpener(
            json.dumps({"items": [svo_item(t_at=None, t_at_mar=None)]}).encode()
        )

        with self.assertRaises(SourceError):
            fetch_flight("SU1032", date(2026, 6, 28), opener=opener)

        self.assertEqual(len(opener.urls), 2)

    def test_update_is_idempotent_and_preserves_known_times(self):
        normalized = normalize_svo_item("SU1032", svo_item())
        base = {"days": [], "statistics": {}}
        now = datetime(2026, 6, 28, 20, tzinfo=timezone.utc)
        once, changed = update_dataset(base, [normalized], now=now)
        self.assertTrue(changed)
        twice, changed_again = update_dataset(once, [normalized], now=now)
        self.assertFalse(changed_again)
        self.assertEqual(once, twice)

        partial = normalize_svo_item("SU1032", svo_item(t_at=None, t_at_mar=None))
        preserved, _ = update_dataset(once, [partial], now=now)
        self.assertEqual(
            preserved["days"][0]["SU1032"]["actualArrival"],
            "2026-06-28T06:52:00Z",
        )

    def test_update_keeps_verified_fr24_backfill_and_removes_unknown_sources(self):
        legacy = {
            "days": [
                {
                    "date": "2026-06-27",
                    "SU1032": {"status": "landed", "source": "legacy-source"},
                },
                {
                    "date": "2026-06-28",
                    "SU1032": {"status": "landed", "source": "fr24-fallback"},
                },
            ],
            "statistics": {},
        }

        updated, changed = update_dataset(legacy, [])

        self.assertTrue(changed)
        self.assertEqual([row["date"] for row in updated["days"]], ["2026-06-28"])

    def test_svo_has_priority_over_fr24_fallback(self):
        svo = normalize_svo_item("SU1032", svo_item())
        fallback = {
            "date": "2026-06-28",
            "flight": "SU1032",
            "record": {
                "status": "landed",
                "actualDeparture": "2026-06-28T05:00:00Z",
                "actualArrival": "2026-06-28T07:00:00Z",
                "source": "fr24-fallback",
            },
        }

        with_svo, _ = update_dataset({"days": [], "statistics": {}}, [svo])
        unchanged, changed = update_dataset(with_svo, [fallback])

        self.assertFalse(changed)
        self.assertEqual(unchanged, with_svo)

    def test_svo_replaces_older_fr24_fallback(self):
        fallback = {
            "days": [
                {
                    "date": "2026-06-28",
                    "SU1032": {
                        "status": "cancelled",
                        "scheduledDeparture": "2026-06-28T04:25:00Z",
                        "source": "fr24-fallback",
                    },
                }
            ],
            "statistics": {},
        }

        updated, changed = update_dataset(
            fallback, [normalize_svo_item("SU1032", svo_item())]
        )

        self.assertTrue(changed)
        record = updated["days"][0]["SU1032"]
        self.assertEqual(record["status"], "landed")
        self.assertEqual(record["source"], "svo")

    def test_statistics_clamp_early_arrivals_and_exclude_unknown(self):
        days = [
            {
                "date": "2026-01-03",
                "SU1032": {
                    "status": "landed",
                    "departureDelayMinutes": 40,
                    "arrivalDelayMinutes": -10,
                },
            },
            {
                "date": "2026-01-02",
                "SU1032": {
                    "status": "landed",
                    "departureDelayMinutes": -5,
                    "arrivalDelayMinutes": 20,
                },
            },
            {"date": "2026-01-01", "SU1032": {"status": "cancelled"}},
            {"date": "2025-12-31", "SU1032": {"status": "unknown"}},
        ]
        stats = compute_statistics(days)["SU1032"]
        self.assertEqual(stats["averageDepartureDelayMinutes"], 20)
        self.assertEqual(stats["averageArrivalDelayMinutes"], 10)
        self.assertEqual(stats["cancellationProbabilityPercent"], 33.3)

    def test_atomic_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "flights.json"
            dataset = {"days": [], "statistics": {}, "schemaVersion": 1}
            write_dataset(path, dataset)
            self.assertEqual(read_dataset(path), dataset)
            self.assertFalse(path.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
