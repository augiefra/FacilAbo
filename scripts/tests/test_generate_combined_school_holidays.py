from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "generate_combined_school_holidays.py"
SUPPLEMENTAL_SOURCE_PATH = (
    Path(__file__).resolve().parents[2] / "sources" / "france-vacances-scolaires-2027-2028.json"
)
SPEC = importlib.util.spec_from_file_location("combined_school_holidays", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def event(summary: str, start: str, end: str):
    return MODULE.SourceEvent(
        summary=summary,
        start=date.fromisoformat(start),
        end=date.fromisoformat(end),
    )


class CombinedSchoolHolidaysTests(unittest.TestCase):
    def test_official_2027_2028_supplement_has_eight_periods_and_one_marker(self) -> None:
        zone_events = MODULE.parse_supplemental_source(SUPPLEMENTAL_SOURCE_PATH)

        self.assertEqual({zone: len(events) for zone, events in zone_events.items()}, {"A": 5, "B": 5, "C": 5})
        self.assertEqual(
            [(item.start, item.end) for item in zone_events["A"]],
            [
                (date(2027, 10, 23), date(2027, 11, 8)),
                (date(2027, 12, 18), date(2028, 1, 3)),
                (date(2028, 2, 19), date(2028, 3, 6)),
                (date(2028, 4, 22), date(2028, 5, 9)),
                (date(2028, 7, 4), date(2028, 7, 5)),
            ],
        )
        self.assertEqual(
            [(item.start, item.end) for item in zone_events["B"]],
            [
                (date(2027, 10, 23), date(2027, 11, 8)),
                (date(2027, 12, 18), date(2028, 1, 3)),
                (date(2028, 2, 5), date(2028, 2, 21)),
                (date(2028, 4, 8), date(2028, 4, 24)),
                (date(2028, 7, 4), date(2028, 7, 5)),
            ],
        )
        self.assertEqual(
            [(item.start, item.end) for item in zone_events["C"]],
            [
                (date(2027, 10, 23), date(2027, 11, 8)),
                (date(2027, 12, 18), date(2028, 1, 3)),
                (date(2028, 2, 12), date(2028, 2, 28)),
                (date(2028, 4, 15), date(2028, 5, 2)),
                (date(2028, 7, 4), date(2028, 7, 5)),
            ],
        )

        combined = MODULE.combine_zone_events(zone_events)
        future = [item for item in combined if item.start >= date(2027, 10, 1)]
        self.assertEqual(len(future), 13)
        summer_markers = [item for item in future if item.start == date(2028, 7, 4)]
        self.assertEqual(len(summer_markers), 1)
        self.assertEqual(summer_markers[0].end, date(2028, 7, 5))
        self.assertEqual(summer_markers[0].zones, frozenset({"A", "B", "C"}))

    def test_staggered_period_is_split_by_active_zone_set(self) -> None:
        combined = MODULE.combine_zone_events(
            {
                "A": [event("Vacances d'Hiver", "2026-02-07", "2026-02-23")],
                "B": [event("Vacances d'Hiver", "2026-02-14", "2026-03-02")],
                "C": [event("Vacances d'Hiver", "2026-02-21", "2026-03-09")],
            }
        )

        self.assertEqual(
            [(item.start, item.end, item.zones) for item in combined],
            [
                (date(2026, 2, 7), date(2026, 2, 14), frozenset({"A"})),
                (date(2026, 2, 14), date(2026, 2, 21), frozenset({"A", "B"})),
                (date(2026, 2, 21), date(2026, 2, 23), frozenset({"A", "B", "C"})),
                (date(2026, 2, 23), date(2026, 3, 2), frozenset({"B", "C"})),
                (date(2026, 3, 2), date(2026, 3, 9), frozenset({"C"})),
            ],
        )

    def test_identical_period_is_emitted_once_for_all_zones(self) -> None:
        common = event("Vacances de Noël", "2026-12-19", "2027-01-04")
        combined = MODULE.combine_zone_events({"A": [common], "B": [common], "C": [common]})

        self.assertEqual(len(combined), 1)
        self.assertEqual(combined[0].zones, frozenset({"A", "B", "C"}))

        calendar_text = MODULE.build_calendar(combined, "20260716T000000Z")
        self.assertIn("SUMMARY:Vacances de Noël — toutes zones", calendar_text)
        self.assertIn("DTSTAMP:20260716T000000Z", calendar_text)
        self.assertEqual(calendar_text.count("BEGIN:VEVENT"), 1)

    def test_existing_uid_dtstamp_is_preserved_during_regeneration(self) -> None:
        combined = [
            MODULE.CombinedEvent(
                summary="Vacances de Noël",
                start=date(2026, 12, 19),
                end=date(2027, 1, 4),
                zones=frozenset({"A", "B", "C"}),
            )
        ]

        uid = MODULE.combined_event_uid(combined[0])
        calendar_text = MODULE.build_calendar(
            combined,
            "20260829T120000Z",
            {uid: "20260316T133328Z"},
        )

        self.assertIn("DTSTAMP:20260316T133328Z", calendar_text)
        self.assertNotIn("DTSTAMP:20260829T120000Z", calendar_text)

    def test_teacher_preentry_is_excluded_and_zero_length_marker_is_one_day(self) -> None:
        raw = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTAMP:20260716T000000Z
DTSTART;VALUE=DATE:20270831
DTEND;VALUE=DATE:20270903
SUMMARY:Vacances d'Été(prérentrée Enseignants)
END:VEVENT
BEGIN:VEVENT
DTSTAMP:20260716T000001Z
DTSTART;VALUE=DATE:20270703
DTEND;VALUE=DATE:20270703
SUMMARY:Début des Vacances d'Été
END:VEVENT
END:VCALENDAR
"""

        events, dtstamp = MODULE.parse_source(raw)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].summary, "Début des Vacances d'Été")
        self.assertEqual(events[0].end, date(2027, 7, 4))
        self.assertEqual(dtstamp, "20260716T000001Z")

    def test_anchor_manifest_uses_the_same_stable_uid_contract(self) -> None:
        common = MODULE.CombinedEvent(
            summary="Vacances de Noël",
            start=date(2026, 12, 19),
            end=date(2027, 1, 4),
            zones=frozenset({"A", "B", "C"}),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "anchors.json"
            path.write_text(json.dumps({"feeds": {}}), encoding="utf-8")

            MODULE.update_anchor_manifest(path, [common])

            entry = json.loads(path.read_text(encoding="utf-8"))["feeds"][
                "education/vacances-toutes-zones.ics"
            ]["events"][0]
            self.assertEqual(
                entry,
                {
                    "uid": "vacances-toutes-zones-vacances-de-noel-20261219-20270104-abc@facilabo.app",
                    "dtstart": "20261219",
                    "dtend": "20270104",
                },
            )

    def test_generated_calendar_has_no_simultaneous_events(self) -> None:
        calendar_path = Path(__file__).resolve().parents[2] / "education" / "vacances-toutes-zones.ics"
        events, _ = MODULE.parse_source(calendar_path.read_text(encoding="utf-8"))
        ordered = sorted(events, key=lambda item: (item.start, item.end))

        self.assertGreater(len(ordered), 0)
        for current, following in zip(ordered, ordered[1:]):
            self.assertLessEqual(current.end, following.start)


if __name__ == "__main__":
    unittest.main()
