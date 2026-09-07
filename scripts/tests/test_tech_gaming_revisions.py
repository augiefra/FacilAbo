"""Protect subscriber revisions when regenerating the tech/gaming calendar."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
BUNDLED_NODE = Path('/opt/homebrew/opt/node@22/bin/node')
NODE = os.environ.get('NODE_EXECUTABLE') or (
    str(BUNDLED_NODE) if BUNDLED_NODE.exists() else shutil.which('node')
)


def events(calendar):
    return {
        re.search(r'^UID:(.*)$', block, re.MULTILINE)[1]: block
        for block in re.findall(r'BEGIN:VEVENT\n.*?END:VEVENT', calendar, re.DOTALL)
    }


def property_value(block, name):
    match = re.search(r'^' + name + r':(.+)$', block, re.MULTILINE)
    return match[1] if match else None


class TechGamingRevisionsTests(unittest.TestCase):
    def test_regeneration_preserves_and_increments_subscriber_revisions(self):
        self.assertIsNotNone(NODE, 'Node.js 22+ is required')
        original = (ROOT / 'culture/tech-gaming.ics').read_text()
        config = json.loads(
            (ROOT / 'sources/tech-gaming-watchlist.yaml').read_text()
        )
        uid = 'culture-tech-gaming-google-cloud-next-2027-2027@facilabo.app'
        old_events = events(original)
        previous_sequence = int(property_value(old_events[uid], 'SEQUENCE') or 0)
        self.assertIsNotNone(property_value(old_events[uid], 'LAST-MODIFIED'))

        with tempfile.TemporaryDirectory(prefix='tech-revision-test-') as directory:
            temporary = Path(directory)
            feed, mirror = temporary / 'feed.ics', temporary / 'mirror.ics'
            config_path = temporary / 'watchlist.json'
            feed.write_text(original)

            def generate():
                config_path.write_text(json.dumps(config))
                subprocess.run(
                    [NODE, '--experimental-strip-types', str(
                        ROOT / 'scripts/update-tech-gaming-calendar.ts'
                    ), '--file', str(config_path), '--output', str(feed),
                     '--mirror', str(mirror)],
                    check=True, capture_output=True, text=True,
                )
                self.assertEqual(feed.read_bytes(), mirror.read_bytes())
                return feed.read_text()

            # No source change must preserve both content and revision timestamps.
            self.assertEqual(generate(), original)
            self.assertEqual(generate(), original)
            source = next(item for item in config['sources']
                          if item['slug'] == 'google-cloud-next-2027')
            source['event']['status'] = (
                'CONFIRMED' if source['event']['status'] != 'CONFIRMED' else 'TENTATIVE'
            )
            source['event']['start_date'] = '2027-04-14'
            changed = generate()
            next_events = events(changed)
            self.assertEqual(set(next_events), set(old_events))
            revised = next_events[uid]
            self.assertEqual(int(property_value(revised, 'SEQUENCE')), previous_sequence + 1)
            self.assertIn('DTSTART;VALUE=DATE:20270414', revised)
            self.assertEqual(property_value(revised, 'STATUS'), source['event']['status'])
            self.assertEqual(property_value(revised, 'DTSTAMP'),
                             property_value(revised, 'LAST-MODIFIED'))
            self.assertEqual(generate(), changed)
            for other_uid, block in old_events.items():
                if other_uid != uid:
                    self.assertEqual(next_events[other_uid], block)

            source['event']['summary'] += ' - information actualisée'
            self.assertEqual(int(property_value(events(generate())[uid], 'SEQUENCE')),
                             previous_sequence + 2)


if __name__ == '__main__':
    unittest.main()
