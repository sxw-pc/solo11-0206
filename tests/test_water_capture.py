import copy
import json
import unittest
from pathlib import Path

from paper_web.store import connect, current, transaction
from paper_web.water_capture import receive, report


class WaterCaptureTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:', create=True)
        self.addCleanup(self.db.close)
        path = Path(__file__).resolve().parents[1] / 'fixtures' / 'water-sampler.json'
        self.packet = json.loads(path.read_text(encoding='utf-8'))

    def test_retains_original_packet_and_separates_waste_from_bottled_doses(self):
        accepted = receive(self.db, self.packet)
        self.assertEqual(accepted['inserted'], 1)
        self.assertEqual([item['volume_ml'] for item in accepted['bottles']], [25, 35])
        self.assertEqual(receive(self.db, self.packet)['replayed'], 1)
        with transaction(self.db, write=False):
            self.assertEqual(report(self.db, self.packet['id'])['bottles'], accepted['bottles'])
            self.assertEqual(json.loads(current(self.db, 'water_packet')[0]['packet']), self.packet)

    def test_packet_with_an_unobserved_boundary_does_not_enter_the_ledger(self):
        bad = copy.deepcopy(self.packet)
        bad['bottles'][0]['close'] = 11
        with self.assertRaisesRegex(ValueError, 'missing counter boundary: east-1'):
            receive(self.db, bad)
        self.assertEqual(current(self.db, 'water_packet'), [])

    def test_revised_capacity_validated_before_capture(self):
        receive(self.db, self.packet)
        revised = copy.deepcopy(self.packet)
        revised['rev'] = 2
        revised['bottles'][1]['capacity_ml'] = 30
        with self.assertRaisesRegex(ValueError, 'bottle volume outside capacity: east-2'):
            receive(self.db, revised)
        self.assertEqual(current(self.db, 'water_packet')[0]['rev'], 1)
