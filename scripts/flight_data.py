"""Fetch and normalize the two tracked Aeroflot flights.

Only Python's standard library is used so the collector runs unchanged on a
GitHub-hosted runner. All persisted instants are UTC ISO-8601 strings.
"""

from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


API_URL = "https://www.svo.aero/bitrix/timetable/"
PUBLIC_TIMETABLE_URL = "https://www.svo.aero/ru/timetable"
UTC = timezone.utc


def _zone(name: str, fallback_hours: int):
    """Use IANA data when present and fixed Russian offsets on minimal Windows."""

    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=fallback_hours), name=name)


MOSCOW = _zone("Europe/Moscow", 3)
KALININGRAD = _zone("Europe/Kaliningrad", 2)
TIMEZONES = {
    "Europe/Moscow": MOSCOW,
    "Europe/Kaliningrad": KALININGRAD,
}

FLIGHTS: dict[str, dict[str, str]] = {
    "SU1032": {
        "number": "1032",
        "direction": "departure",
        "departureAirport": "SVO",
        "arrivalAirport": "KGD",
        "departureTimezone": "Europe/Moscow",
        "arrivalTimezone": "Europe/Kaliningrad",
    },
    "SU1009": {
        "number": "1009",
        "direction": "arrival",
        "departureAirport": "KGD",
        "arrivalAirport": "SVO",
        "departureTimezone": "Europe/Kaliningrad",
        "arrivalTimezone": "Europe/Moscow",
    },
}

TIMING_FIELDS = (
    "scheduledDeparture",
    "actualDeparture",
    "estimatedDeparture",
    "scheduledArrival",
    "actualArrival",
    "estimatedArrival",
)

STATUS_RANK = {
    "unknown": 0,
    "scheduled": 1,
    "airborne": 2,
    "landed": 3,
    "diverted": 4,
    "cancelled": 5,
}

SOURCE_PRIORITY = {
    "fr24-seed": 1,
    "fr24-fallback": 1,
    "svo": 2,
}


class SourceError(RuntimeError):
    """Raised when the SVO board cannot be read or decoded."""


def _iso_utc(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MOSCOW)
    utc_value = parsed.astimezone(UTC).replace(microsecond=0)
    return utc_value.isoformat().replace("+00:00", "Z")


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _minutes_between(start: str | None, end: str | None) -> int | None:
    start_dt = _parse_utc(start)
    end_dt = _parse_utc(end)
    if not start_dt or not end_dt:
        return None
    return round((end_dt - start_dt).total_seconds() / 60)


def _status(item: Mapping[str, Any], actual_departure: str | None, actual_arrival: str | None) -> str:
    text = " ".join(
        str(item.get(key) or "")
        for key in ("vip_status_rus", "vip_status_eng", "vip_status")
    ).lower()
    status_id = str(item.get("status_id") or "")
    if status_id == "230" or "отмен" in text or "cancel" in text:
        return "cancelled"
    if any(marker in text for marker in ("перенаправ", "запасн", "divert")):
        return "diverted"
    if actual_arrival:
        return "landed"
    if actual_departure:
        return "airborne"
    return "scheduled"


def normalize_svo_item(flight_code: str, item: Mapping[str, Any]) -> dict[str, Any]:
    """Convert one SVO flight-detail object into the public data schema."""

    if flight_code not in FLIGHTS:
        raise ValueError(f"Unsupported flight: {flight_code}")

    if flight_code == "SU1032":
        scheduled_departure = _iso_utc(item.get("t_st"))
        actual_departure = _iso_utc(item.get("t_at"))
        estimated_departure = _iso_utc(item.get("t_et"))
        scheduled_arrival = _iso_utc(item.get("t_st_mar"))
        actual_arrival = _iso_utc(item.get("t_at_mar") or item.get("mar2_at"))
        estimated_arrival = _iso_utc(item.get("marArrivalEt"))
    else:
        scheduled_departure = _iso_utc(item.get("t_st_mar"))
        actual_departure = _iso_utc(item.get("t_at_mar") or item.get("mar1_dt"))
        estimated_departure = None
        scheduled_arrival = _iso_utc(item.get("t_st"))
        actual_arrival = _iso_utc(item.get("t_at"))
        estimated_arrival = _iso_utc(item.get("t_et"))

    service_date = str(item.get("dat") or "")[:10]
    if not service_date and scheduled_departure:
        config = FLIGHTS[flight_code]
        service_date = _parse_utc(scheduled_departure).astimezone(
            TIMEZONES[config["departureTimezone"]]
        ).date().isoformat()

    status = _status(item, actual_departure, actual_arrival)
    departure_reference = actual_departure or estimated_departure
    arrival_reference = actual_arrival or estimated_arrival

    source_flight_id = str(item.get("i_id") or "")
    direction = FLIGHTS[flight_code]["direction"]
    source_url = (
        f"{PUBLIC_TIMETABLE_URL}/{direction}/flight/{source_flight_id}/info"
        if source_flight_id
        else None
    )

    record = {
        "status": status,
        "scheduledDeparture": scheduled_departure,
        "actualDeparture": actual_departure,
        "estimatedDeparture": estimated_departure,
        "scheduledArrival": scheduled_arrival,
        "actualArrival": actual_arrival,
        "estimatedArrival": estimated_arrival,
        "durationMinutes": _minutes_between(actual_departure, actual_arrival),
        "departureDelayMinutes": _minutes_between(scheduled_departure, departure_reference),
        "arrivalDelayMinutes": _minutes_between(scheduled_arrival, arrival_reference),
        "source": "svo",
        "sourceFlightId": source_flight_id or None,
        "sourceUrl": source_url,
    }
    return {"date": service_date, "flight": flight_code, "record": record}


def _day_bounds(day: date) -> tuple[str, str]:
    start = datetime.combine(day, time.min, MOSCOW)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def build_url(flight_code: str, day: date) -> str:
    config = FLIGHTS[flight_code]
    start, end = _day_bounds(day)
    query = urllib.parse.urlencode(
        {
            "search": flight_code,
            "direction": config["direction"],
            "dateStart": start,
            "dateEnd": end,
            "perPage": 20,
            "page": 0,
        }
    )
    return f"{API_URL}?{query}"


def build_detail_url(source_flight_id: str) -> str:
    return f"{API_URL}{urllib.parse.quote(source_flight_id, safe='')}/"


def _read_svo_json(
    url: str,
    opener: Callable[..., Any],
    timeout: int,
    context: str,
) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "aeroflot-flight-tracker/1.0 (+GitHub Pages)",
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            return json.load(response)
    except Exception as exc:  # urllib exposes several environment-specific errors
        raise SourceError(f"SVO request failed for {context}: {exc}") from exc


def fetch_flight(
    flight_code: str,
    day: date,
    opener: Callable[..., Any] = urllib.request.urlopen,
    timeout: int = 20,
) -> dict[str, Any] | None:
    payload = _read_svo_json(
        build_url(flight_code, day), opener, timeout, f"{flight_code} {day} board"
    )

    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise SourceError(f"Unexpected SVO response for {flight_code} {day}")

    config = FLIGHTS[flight_code]
    for item in items:
        carrier = item.get("co") if isinstance(item, dict) else None
        if not isinstance(carrier, dict):
            continue
        if carrier.get("code") == "SU" and str(item.get("flt")) == config["number"]:
            source_flight_id = str(item.get("i_id") or "")
            if not source_flight_id:
                raise SourceError(f"SVO flight id missing for {flight_code} {day}")
            detail = _read_svo_json(
                build_detail_url(source_flight_id),
                opener,
                timeout,
                f"{flight_code} {day} detail {source_flight_id}",
            )
            if not isinstance(detail, dict):
                raise SourceError(f"Unexpected SVO detail response for {flight_code} {day}")
            detail_carrier = detail.get("co")
            if (
                not isinstance(detail_carrier, dict)
                or detail_carrier.get("code") != "SU"
                or str(detail.get("flt")) != config["number"]
            ):
                raise SourceError(f"Mismatched SVO detail response for {flight_code} {day}")
            return normalize_svo_item(flight_code, detail)
    return None


def _trusted_source_days(days: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep records from SVO and the explicitly verified FR24 backfill."""

    cleaned: list[dict[str, Any]] = []
    for day in days:
        service_date = str(day.get("date") or "")
        if not service_date:
            continue
        row: dict[str, Any] = {"date": service_date}
        for flight_code in FLIGHTS:
            record = day.get(flight_code)
            if (
                isinstance(record, Mapping)
                and record.get("source") in SOURCE_PRIORITY
            ):
                row[flight_code] = deepcopy(dict(record))
        if len(row) > 1:
            cleaned.append(row)
    return cleaned


def _merge_record(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> dict[str, Any]:
    if not old:
        return deepcopy(dict(new))

    old_priority = SOURCE_PRIORITY.get(str(old.get("source") or ""), 0)
    new_priority = SOURCE_PRIORITY.get(str(new.get("source") or ""), 0)
    if new_priority < old_priority:
        return deepcopy(dict(old))

    merged = deepcopy(dict(old))
    for key, value in new.items():
        if value is not None:
            merged[key] = value

    old_status = str(old.get("status") or "unknown")
    new_status = str(new.get("status") or "unknown")
    if (
        new_priority > old_priority
        or STATUS_RANK.get(new_status, 0) >= STATUS_RANK.get(old_status, 0)
    ):
        merged["status"] = new_status

    merged["durationMinutes"] = _minutes_between(
        merged.get("actualDeparture"), merged.get("actualArrival")
    )
    departure_reference = merged.get("actualDeparture") or merged.get("estimatedDeparture")
    arrival_reference = merged.get("actualArrival") or merged.get("estimatedArrival")
    merged["departureDelayMinutes"] = _minutes_between(
        merged.get("scheduledDeparture"), departure_reference
    )
    merged["arrivalDelayMinutes"] = _minutes_between(
        merged.get("scheduledArrival"), arrival_reference
    )
    return merged


def compute_statistics(days: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    rows = list(days)
    for flight_code in FLIGHTS:
        landed: list[Mapping[str, Any]] = []
        cancelled = 0
        diverted = 0
        unknown = 0
        for day in rows:
            record = day.get(flight_code)
            if not isinstance(record, Mapping):
                continue
            status = record.get("status")
            if status == "landed":
                landed.append(record)
            elif status == "cancelled":
                cancelled += 1
            elif status == "diverted":
                diverted += 1
            elif status == "unknown":
                unknown += 1

        def average_delay(field: str) -> int | None:
            values = [
                max(0, int(record[field]))
                for record in landed
                if record.get(field) is not None
            ]
            return round(sum(values) / len(values)) if values else None

        resolved = len(landed) + cancelled + diverted
        probability = round(cancelled / resolved * 100, 1) if resolved else None
        result[flight_code] = {
            "landedFlights": len(landed),
            "cancelledFlights": cancelled,
            "divertedFlights": diverted,
            "unknownFlights": unknown,
            "averageDepartureDelayMinutes": average_delay("departureDelayMinutes"),
            "averageArrivalDelayMinutes": average_delay("arrivalDelayMinutes"),
            "cancellationProbabilityPercent": probability,
        }
    return result


def update_dataset(
    dataset: Mapping[str, Any],
    normalized_records: Iterable[Mapping[str, Any]],
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    updated = deepcopy(dict(dataset))
    original_days = updated.get("days", [])
    source_days = _trusted_source_days(
        original_days if isinstance(original_days, list) else []
    )
    by_date = {row["date"]: deepcopy(row) for row in source_days}
    changed = original_days != source_days

    for normalized in normalized_records:
        service_date = str(normalized.get("date") or "")
        flight_code = str(normalized.get("flight") or "")
        record = normalized.get("record")
        if not service_date or flight_code not in FLIGHTS or not isinstance(record, Mapping):
            continue
        day_row = by_date.setdefault("" + service_date, {"date": service_date})
        old_record = day_row.get(flight_code)
        merged = _merge_record(old_record if isinstance(old_record, Mapping) else None, record)
        if old_record != merged:
            day_row[flight_code] = merged
            changed = True

    updated["days"] = sorted(by_date.values(), key=lambda row: row["date"], reverse=True)
    updated["statistics"] = compute_statistics(updated["days"])
    if changed:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
        updated["updatedAt"] = timestamp.isoformat().replace("+00:00", "Z")
    return updated, changed


def read_dataset(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_dataset(path: Path, dataset: Mapping[str, Any]) -> None:
    serialized = json.dumps(dataset, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(serialized, encoding="utf-8")
    temporary.replace(path)
