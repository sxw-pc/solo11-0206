import copy
import json
import unittest
from pathlib import Path

from paper_web.fibre_capture import receive, report
from paper_web.store import connect, current


class FibreCaptureTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:', create=True)
        self.addCleanup(self.db.close)
        fixture = Path(__file__).resolve().parents[1] / 'fixtures' / 'fibre-tray.json'
        self.image = json.loads(fixture.read_text(encoding='utf-8'))

    def test_receives_calibrated_traces_and_replays_the_original(self):
        accepted = receive(self.db, self.image)
        self.assertEqual(accepted['histogram'], [{'length_um': 50, 'count': 2},
                                                  {'length_um': 60, 'count': 1}])
        self.assertEqual(receive(self.db, self.image)['replayed'], 1)
        self.assertEqual(report(self.db, self.image['id'])['histogram'], accepted['histogram'])
        self.assertEqual(json.loads(current(self.db, 'fibre_image')[0]['image']), self.image)

    def test_rejects_duplicate_trace_before_inserting_revision(self):
        receive(self.db, self.image)
        bad = copy.deepcopy(self.image)
        bad['rev'] = 2
        bad['traces'].append(bad['traces'][0])
        with self.assertRaisesRegex(ValueError, 'duplicate or empty trace'):
            receive(self.db, bad)
        self.assertEqual(current(self.db, 'fibre_image')[0]['rev'], 1)
