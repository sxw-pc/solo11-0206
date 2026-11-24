import copy
import json
import unittest
from pathlib import Path

from paper_web.archive_capture import receive, restore
from paper_web.store import connect, current


class ArchiveCaptureTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:', create=True)
        self.addCleanup(self.db.close)
        path = Path(__file__).resolve().parents[1] / 'fixtures' / 'archive-read.json'
        self.packet = json.loads(path.read_text(encoding='utf-8'))

    def test_round_trip_replays_block_from_real_cartridge(self):
        self.assertEqual(receive(self.db, self.packet)['inserted'], 1)
        self.assertEqual(receive(self.db, self.packet)['replayed'], 1)
        restored = restore(self.db, 'read-1', 'log-head')
        self.assertEqual((restored['run'], restored['cartridge'], restored['hex']),
                         ('press-114', 'cartridge-dry-1', '616263'))

    def test_mismatched_block_does_not_bump_capture_revision(self):
        receive(self.db, self.packet)
        bad = copy.deepcopy(self.packet)
        bad['rev'] = 2
        bad['blocks'][0]['hex'] = '646566'
        with self.assertRaisesRegex(ValueError, 'checksum mismatch: log-head'):
            receive(self.db, bad)
        self.assertEqual(current(self.db, 'cartridge_read')[0]['rev'], 1)
