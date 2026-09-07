#!/usr/bin/env python3
"""Generate one readable metropolitan school-holidays calendar from zones A/B/C."""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


DEFAULT_SOURCES = {
    "A": "https://fr.ftp.opendatasoft.com/openscol/fr-en-calendrier-scolaire/Zone-A.ics",
    "B": "https://fr.ftp.opendatasoft.com/openscol/fr-en-calendrier-scolaire/Zone-B.ics",
    "C": "https://fr.ftp.opendatasoft.com/openscol/fr-en-calendrier-scolaire/Zone-C.ics",
}
DEFAULT_SUPPLEMENTAL_SOURCE = "sources/france-vacances-scolaires-2027-2028.json"


@dataclass(frozen=True)
class SourceEvent:
    summary: str
    start: date
    end: date


@dataclass(frozen=True)
class CombinedEvent:
    summary: str
    start: date
    end: date
    zones: frozenset[str]


def unfold_ical(raw_text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line.startswith((" ", "\t")) and lines:
            lines[-1] += raw_line[1:]
        else:
            lines.append(raw_line)
    return lines


def field_value(lines: Iterable[str], field: str) -> str | None:
    prefix = f"{field}:"
    parameter_prefix = f"{field};"
    for line in lines:
        if line.startswith(prefix) or line.startswith(parameter_prefix):
            return line.split(":", 1)[1]
    return None


def parse_date(value: str) -> date:
    match = re.match(r"^(\d{8})", value)
    if not match:
        raise ValueError(f"Unsupported ICS date: {value}")
    return datetime.strptime(match.group(1), "%Y%m%d").date()


def normalized_key(summary: str) -> str:
    folded = unicodedata.normalize("NFKD", summary)
    ascii_summary = "".join(char for char in folded if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "-", ascii_summary.casefold()).strip("-")


def combined_event_uid(event: CombinedEvent) -> str:
    zones_key = "".join(sorted(event.zones)).lower()
    summary_key = normalized_key(event.summary)
    return (
        f"vacances-toutes-zones-{summary_key}-{event.start:%Y%m%d}-"
        f"{event.end:%Y%m%d}-{zones_key}@facilabo.app"
    )


def parse_source(raw_text: str) -> tuple[list[SourceEvent], str]:
    lines = unfold_ical(raw_text)
    events: list[SourceEvent] = []
    dtstamps: list[str] = []
    current: list[str] | None = None

    for line in lines:
        if line == "BEGIN:VEVENT":
            current = []
        elif line == "END:VEVENT" and current is not None:
            summary = field_value(current, "SUMMARY")
            start_raw = field_value(current, "DTSTART")
            end_raw = field_value(current, "DTEND")
            dtstamp = field_value(current, "DTSTAMP")
            current = None

            if not summary or not start_raw or not end_raw:
                continue
            if "enseignant" in normalized_key(summary):
                continue

            start = parse_date(start_raw)
            end = parse_date(end_raw)
            if end < start:
                raise ValueError(f"DTEND precedes DTSTART for {summary}: {start} -> {end}")
            if end == start:
                end = start + timedelta(days=1)

            events.append(SourceEvent(summary=summary.strip(), start=start, end=end))
            if dtstamp and re.match(r"^\d{8}T\d{6}Z$", dtstamp):
                dtstamps.append(dtstamp)
        elif current is not None:
            current.append(line)

    if not events:
        raise ValueError("No usable VEVENT found in source calendar")
    return events, max(dtstamps, default="19700101T000000Z")


def parse_supplemental_source(path: Path) -> dict[str, list[SourceEvent]]:
    """Load an official static supplement when the upstream ICS has not caught up."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("source"), dict):
        raise ValueError(f"Invalid supplemental source metadata: {path}")
    if not payload["source"].get("url"):
        raise ValueError(f"Supplemental source URL is required: {path}")

    raw_events = payload.get("events")
    if not isinstance(raw_events, list) or not raw_events:
        raise ValueError(f"Supplemental source has no events: {path}")

    expected_zones = set(DEFAULT_SOURCES)
    zone_events: dict[str, list[SourceEvent]] = {zone: [] for zone in expected_zones}
    for index, raw_event in enumerate(raw_events):
        if not isinstance(raw_event, dict):
            raise ValueError(f"Invalid supplemental event #{index + 1}: {path}")

        summary = raw_event.get("summary")
        zones = raw_event.get("zones")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError(f"Supplemental event #{index + 1} has no summary: {path}")
        if (
            not isinstance(zones, list)
            or not zones
            or len(set(zones)) != len(zones)
            or not set(zones).issubset(expected_zones)
        ):
            raise ValueError(f"Invalid zones for supplemental event #{index + 1}: {path}")

        kind = raw_event.get("kind", "period")
        if kind == "marker":
            marker = raw_event.get("date")
            if not isinstance(marker, str):
                raise ValueError(f"Marker has no date for supplemental event #{index + 1}: {path}")
            try:
                start = date.fromisoformat(marker)
            except ValueError as error:
                raise ValueError(f"Invalid marker date {marker!r}: {path}") from error
            end = start + timedelta(days=1)
        elif kind == "period":
            start_raw = raw_event.get("start")
            end_raw = raw_event.get("end")
            if not isinstance(start_raw, str) or not isinstance(end_raw, str):
                raise ValueError(f"Period has no start/end for supplemental event #{index + 1}: {path}")
            try:
                start = date.fromisoformat(start_raw)
                end = date.fromisoformat(end_raw)
            except ValueError as error:
                raise ValueError(f"Invalid period dates for supplemental event #{index + 1}: {path}") from error
            if end <= start:
                raise ValueError(f"Period must have an exclusive end after its start: {path}")
        else:
            raise ValueError(f"Unsupported supplemental event kind {kind!r}: {path}")

        source_event = SourceEvent(summary=summary.strip(), start=start, end=end)
        for zone in zones:
            zone_events[zone].append(source_event)

    return zone_events


def combine_zone_events(zone_events: dict[str, list[SourceEvent]]) -> list[CombinedEvent]:
    expected_zones = set(DEFAULT_SOURCES)
    if set(zone_events) != expected_zones:
        raise ValueError(f"Expected zones {sorted(expected_zones)}, got {sorted(zone_events)}")

    grouped: dict[str, list[tuple[str, SourceEvent]]] = {}
    display_summaries: dict[str, str] = {}
    for zone, events in zone_events.items():
        seen: set[tuple[str, date, date]] = set()
        for event in events:
            key = normalized_key(event.summary)
            signature = (key, event.start, event.end)
            if signature in seen:
                continue
            seen.add(signature)
            grouped.setdefault(key, []).append((zone, event))
            display_summaries.setdefault(key, event.summary)

    combined: list[CombinedEvent] = []
    for key, entries in grouped.items():
        boundaries = sorted({point for _, event in entries for point in (event.start, event.end)})
        segments: list[CombinedEvent] = []

        for start, end in zip(boundaries, boundaries[1:]):
            active_zones = frozenset(
                zone
                for zone, event in entries
                if event.start <= start and event.end >= end
            )
            if not active_zones:
                continue

            if segments and segments[-1].end == start and segments[-1].zones == active_zones:
                previous = segments[-1]
                segments[-1] = CombinedEvent(
                    summary=previous.summary,
                    start=previous.start,
                    end=end,
                    zones=previous.zones,
                )
            else:
                segments.append(
                    CombinedEvent(
                        summary=display_summaries[key],
                        start=start,
                        end=end,
                        zones=active_zones,
                    )
                )

        combined.extend(segments)

    return sorted(combined, key=lambda event: (event.start, event.end, normalized_key(event.summary)))


def zone_label(zones: frozenset[str]) -> str:
    ordered = [zone for zone in ("A", "B", "C") if zone in zones]
    if ordered == ["A", "B", "C"]:
        return "toutes zones"
    if len(ordered) == 1:
        return f"zone {ordered[0]}"
    return f"zones {' et '.join(ordered)}"


def escape_ical_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def fold_ical_line(line: str, limit: int = 73) -> list[str]:
    folded: list[str] = []
    remaining = line
    first = True
    while remaining:
        prefix = "" if first else " "
        byte_limit = limit if first else limit - 1
        chunk = ""
        for char in remaining:
            if len((chunk + char).encode("utf-8")) > byte_limit:
                break
            chunk += char
        if not chunk:
            raise ValueError(f"Unable to fold ICS line: {line!r}")
        folded.append(prefix + chunk)
        remaining = remaining[len(chunk) :]
        first = False
    return folded or [""]


def parse_event_dtstamps(path: Path) -> dict[str, str]:
    """Read existing VEVENT DTSTAMP values so regeneration does not churn history."""
    current: list[str] | None = None
    preserved: dict[str, str] = {}
    for line in unfold_ical(path.read_text(encoding="utf-8")):
        if line == "BEGIN:VEVENT":
            current = []
        elif line == "END:VEVENT" and current is not None:
            uid = field_value(current, "UID")
            dtstamp = field_value(current, "DTSTAMP")
            if uid and dtstamp and re.match(r"^\d{8}T\d{6}Z$", dtstamp):
                preserved[uid] = dtstamp
            current = None
        elif current is not None:
            current.append(line)
    return preserved


def preserve_event_revisions(calendar_text: str, previous_path: Path) -> str:
    """Keep revisions for unchanged UID content; increment an actual material edit."""
    revision_fields = ("DTSTAMP", "LAST-MODIFIED", "SEQUENCE")

    def records(text: str) -> dict[str, dict[str, str]]:
        result = {}
        current = None
        for line in unfold_ical(text):
            if line == "BEGIN:VEVENT":
                current = {}
            elif line == "END:VEVENT" and current is not None:
                result[current["UID"]] = current
                current = None
            elif current is not None:
                key, value = line.split(":", 1)
                current[key] = value
        return result

    old_records = records(previous_path.read_text(encoding="utf-8"))
    revision_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def update(match: re.Match) -> str:
        record = next(iter(records(match.group()).values()))
        old = old_records.get(record["UID"])
        if old is None:
            return match.group()
        material = lambda event: {key: value for key, value in event.items() if key not in revision_fields}
        if material(record) == material(old):
            for key in revision_fields:
                if key in old:
                    record[key] = old[key]
                else:
                    record.pop(key, None)
        else:
            record.update({"DTSTAMP": revision_stamp, "LAST-MODIFIED": revision_stamp,
                           "SEQUENCE": str(int(old.get("SEQUENCE", "0")) + 1)})
        return "\r\n".join(folded for line in ["BEGIN:VEVENT", *[f"{key}:{value}" for key, value in record.items()], "END:VEVENT"]
                            for folded in fold_ical_line(line))

    return re.sub(r"BEGIN:VEVENT\r?\n.*?END:VEVENT", update, calendar_text, flags=re.DOTALL)


def build_calendar(
    events: list[CombinedEvent],
    dtstamp: str,
    preserved_dtstamps: dict[str, str] | None = None,
) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//FacilAbo//Vacances scolaires toutes zones//FR",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "NAME:Vacances scolaires - Toutes zones (FacilAbo)",
        "X-WR-CALNAME:Vacances scolaires - Toutes zones (FacilAbo)",
        "X-WR-TIMEZONE:Europe/Paris",
        "X-WR-CALDESC:Zones A, B et C sans doublons quotidiens.",
    ]

    for event in events:
        uid = combined_event_uid(event)
        label = zone_label(event.zones)
        description = (
            f"Zones concernées : {label}. Segment calculé à partir des calendriers "
            "officiels des zones A, B et C."
        )
        location = "France métropolitaine" if len(event.zones) == 3 else f"France métropolitaine - {label}"

        event_lines = [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{(preserved_dtstamps or {}).get(uid, dtstamp)}",
            f"DTSTART;VALUE=DATE:{event.start:%Y%m%d}",
            f"DTEND;VALUE=DATE:{event.end:%Y%m%d}",
            f"SUMMARY:{escape_ical_text(f'{event.summary} — {label}')}",
            f"DESCRIPTION:{escape_ical_text(description)}",
            f"LOCATION:{escape_ical_text(location)}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
        for line in event_lines:
            lines.extend(fold_ical_line(line))

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def read_source(location: str) -> str:
    if location.startswith(("https://", "http://")):
        request = urllib.request.Request(location, headers={"User-Agent": "FacilAbo-Calendar-Generator/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8-sig")
    return Path(location).read_text(encoding="utf-8-sig")


def update_anchor_manifest(path: Path, events: list[CombinedEvent]) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    feeds = manifest.get("feeds")
    if not isinstance(feeds, dict):
        raise ValueError(f"Invalid date-anchor manifest: {path}")

    combined_path = "education/vacances-toutes-zones.ics"
    combined_spec = {
        "mode": "exact",
        "events": [
            {
                "uid": combined_event_uid(event),
                "dtstart": event.start.strftime("%Y%m%d"),
                "dtend": event.end.strftime("%Y%m%d"),
            }
            for event in events
        ],
    }
    if combined_path in feeds:
        feeds[combined_path] = combined_spec
    else:
        ordered_feeds: dict[str, object] = {}
        education_paths = [key for key in feeds if key.startswith("education/")]
        insert_after = education_paths[-1] if education_paths else None
        for key, value in feeds.items():
            ordered_feeds[key] = value
            if key == insert_after:
                ordered_feeds[combined_path] = combined_spec
        if insert_after is None:
            ordered_feeds[combined_path] = combined_spec
        manifest["feeds"] = ordered_feeds
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    script_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    for zone in ("A", "B", "C"):
        parser.add_argument(f"--zone-{zone.lower()}", default=DEFAULT_SOURCES[zone])
    parser.add_argument(
        "--output",
        default=str(script_root / "education" / "vacances-toutes-zones.ics"),
        help="Canonical ICS output path.",
    )
    parser.add_argument("--mirror-output", help="Optional second output path for the iOS repo mirror.")
    parser.add_argument("--anchor-manifest", help="Optional date-anchor JSON manifest to update.")
    parser.add_argument(
        "--supplemental-source",
        default=str(script_root / DEFAULT_SUPPLEMENTAL_SOURCE),
        help="Optional static official supplement for years missing from the upstream ICS.",
    )
    parser.add_argument(
        "--no-supplemental-source",
        action="store_true",
        help="Do not load the default static official supplement.",
    )
    parser.add_argument(
        "--dtstamp",
        help="Override the VEVENT DTSTAMP (YYYYMMDDTHHMMSSZ).",
    )
    parser.add_argument(
        "--preserve-dtstamps-from",
        help="Existing ICS whose VEVENT DTSTAMP values must be retained by UID.",
    )
    parser.add_argument(
        "--from-year",
        type=int,
        default=2024,
        help="Keep periods ending in or after this year (default: 2024).",
    )
    args = parser.parse_args()

    zone_events: dict[str, list[SourceEvent]] = {}
    dtstamps: list[str] = []
    for zone in ("A", "B", "C"):
        events, dtstamp = parse_source(read_source(getattr(args, f"zone_{zone.lower()}")))
        zone_events[zone] = events
        dtstamps.append(dtstamp)

    if not args.no_supplemental_source:
        supplemental_events = parse_supplemental_source(Path(args.supplemental_source))
        for zone in ("A", "B", "C"):
            zone_events[zone].extend(supplemental_events[zone])

    combined = [
        event
        for event in combine_zone_events(zone_events)
        if event.end > date(args.from_year, 1, 1)
    ]
    dtstamp = args.dtstamp or max(dtstamps)
    if not re.match(r"^\d{8}T\d{6}Z$", dtstamp):
        raise ValueError(f"Invalid DTSTAMP: {dtstamp}")
    preserved_dtstamps = (
        parse_event_dtstamps(Path(args.preserve_dtstamps_from))
        if args.preserve_dtstamps_from
        else {}
    )
    calendar_text = build_calendar(combined, dtstamp, preserved_dtstamps)
    previous_path = Path(args.preserve_dtstamps_from or args.output)
    if previous_path.exists():
        calendar_text = preserve_event_revisions(calendar_text, previous_path)

    output_paths = [Path(args.output)]
    if args.mirror_output:
        output_paths.append(Path(args.mirror_output))
    for output_path in output_paths:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(calendar_text, encoding="utf-8", newline="")
    if args.anchor_manifest:
        update_anchor_manifest(Path(args.anchor_manifest), combined)

    suffix = f"; anchors -> {args.anchor_manifest}" if args.anchor_manifest else ""
    print(f"Generated {len(combined)} events -> {', '.join(str(path) for path in output_paths)}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
