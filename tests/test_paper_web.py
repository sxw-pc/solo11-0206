import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from paper_web.__main__ import build, demo, recompute
from paper_web.finishing import issue, weigh
from paper_web.store import connect, ingest, transaction


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures/demo.json'


class PaperWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(TemporaryDirectory(dir=ROOT))
        self.path = Path(self.tmp) / 'test.sqlite'
        self.db = self.enterContext(closing(connect(self.path, create=True)))
        self.capture = json.loads(FIXTURE.read_text(encoding='utf-8-sig'))['capture']
        self.sample = demo(self.db, FIXTURE)

    def record(self, table, name, **changes):
        data = next(row for row in self.capture if row['type'] == table and row['id'] == name)
        return {**data, 'rev': 2, **changes}

    def test_counter_reset_and_late_collision(self):
        machine = build(self.db)[0]
        self.assertEqual(machine.assess('R2')['box'], (100, 200, 0, 100))
        self.assertEqual(machine.assess('R2')['incidents'][0]['span'], (100, 103))
        ingest(self.db, [self.record('epoch', 'reset', base=99)])
        self.assertEqual(build(self.db)[0].assess('R1')['conflicts'], ['R2'])
        self.assertIn('reel-conflict', build(self.db)[2].assess('A')['reasons'])
        self.assertNotIn('child:A', build(self.db)[3].assess('claim-7')['allocation'])
        self.assertNotIn('child:C', build(self.db)[3].assess('claim-7')['allocation'])

    def test_missing_epoch_and_out_of_domain_incident(self):
        ingest(self.db, [self.record('reel', 'R2', epoch='missing'), self.record('incident', 'join', hi=1121)])
        self.assertEqual(build(self.db)[0].assess('R2')['status'], 'unlocated')
        self.assertIn('unmapped-incident:join', build(self.db)[2].assess('A')['reasons'])

    def test_cross_reel_defect_union_and_unverified_hit(self):
        before = build(self.db)[1].assess('D-cross')
        self.assertEqual(before['area'], 400)
        self.assertEqual([part['area'] for part in before['reels'].values()], [200, 200])
        ingest(self.db, [self.record('hit', 'smudge-2', x1=101)])
        after = build(self.db)[1].assess('D-cross')
        self.assertEqual(after['unverified_hits'], ['smudge-2'])
        self.assertEqual(after['area'], 400)

    def test_knife_conflict_and_cut_union(self):
        ingest(self.db, [{**self.record('cut', 'A-tail', lo=94, hi=99), 'id': 'extra', 'rev': 1}])
        self.assertEqual(build(self.db)[2].plan('A')['removed'], [(94, 100, 0, 50)])
        ingest(self.db, [self.record('lane', 'B', x0=49)])
        self.assertIn('knife-conflict:B', build(self.db)[2].assess('A')['reasons'])

    def test_break_splice_reject_and_scan_gap_are_separate(self):
        finish = build(self.db)[2]
        self.assertIn('splice:join', finish.assess('B')['reasons'])
        self.assertIn('break:tear', finish.assess('D')['reasons'])
        self.assertEqual(finish.assess('D')['missing_area'], 1000)
        self.assertIn({'hit': 'splice-ridge', 'area': 60}, finish.assess('C')['removed_hits'])
        ingest(self.db, [self.record('cut', 'C-head', hi=2)])
        after = build(self.db)[2].assess('C')
        self.assertIn('reject:splice-ridge', after['reasons'])
        self.assertEqual(after['weight'], 'missing')
        self.assertEqual(after['signature'], 'stale')

    def test_weight_tolerance_conflicting_and_recovery(self):
        finish = build(self.db)[2]
        self.assertEqual(finish.assess('A')['weight'], 'accepted')
        self.assertEqual(finish.assess('B')['weight'], 'outside-tolerance')
        weigh(self.db, 'A', 'demo-A', 2, 489, finish.plan('A')['plan'])
        self.assertEqual(build(self.db)[2].assess('A')['weight'], 'outside-tolerance')
        weigh(self.db, 'A', 'demo-A', 3, 500, finish.plan('A')['plan'])
        weigh(self.db, 'A', 'second', 1, 500, finish.plan('A')['plan'])
        self.assertEqual(build(self.db)[2].assess('A')['weight'], 'conflicting')
        weigh(self.db, 'A', 'second', 2, 500, 'wrong-plan')
        self.assertEqual(build(self.db)[2].assess('A')['weight'], 'accepted')

    def test_release_compare_and_swap_and_claim_reallocation(self):
        start = build(self.db)[2].assess('A')
        self.assertEqual(issue(self.db, build, 'A', start['basis'], 'retry'), start['issue'])
        ingest(self.db, [self.record('scan', 'left-camera', verified=0)])
        with self.assertRaisesRegex(ValueError, 'basis changed'):
            issue(self.db, build, 'A', start['basis'], 'operator')
        held = build(self.db)[2].assess('A')
        with self.assertRaisesRegex(ValueError, 'release held'):
            issue(self.db, build, 'A', held['basis'], 'operator')
        self.assertEqual(build(self.db)[3].assess('claim-7')['allocation'], {'removed': 38, 'reserve': 963})
        ingest(self.db, [self.record('scan', 'left-camera', rev=3)])
        fixed = build(self.db)[2].assess('A')
        self.assertEqual(fixed['status'], 'eligible')
        self.assertEqual(fixed['signature'], 'stale')
        self.assertGreater(issue(self.db, build, 'A', fixed['basis'], 'operator'), start['issue'])
        self.assertIn('child:A', build(self.db)[3].assess('claim-7')['allocation'])

    def test_claim_union_cents_and_invalid_sample(self):
        claim = build(self.db)[3].assess('claim-7')
        self.assertEqual(claim['areas'], {'child:A': 50, 'child:C': 70, 'removed': 80, 'reserve': 200})
        self.assertEqual(claim['allocation'], {'child:A': 125, 'child:C': 175, 'removed': 200, 'reserve': 501})
        ingest(self.db, [self.record('sample', 'customer-repeat', verified=0)])
        self.assertEqual(build(self.db)[3].assess('claim-7')['allocation'], {'reserve': 1001})
        self.assertEqual(build(self.db)[3].assess('claim-pending')['status'], 'pending-sample')

    def test_atomic_conflict_and_out_of_order_revision(self):
        conflict = self.record('epoch', 'reset', rev=1, base=110)
        with self.assertRaisesRegex(ValueError, 'conflicting epoch'):
            ingest(self.db, [self.record('epoch', 'reset', base=101), conflict])
        self.assertEqual(build(self.db)[0].epochs['reset']['rev'], 1)
        ingest(self.db, [self.record('epoch', 'reset', rev=3, base=100)])
        ingest(self.db, [self.record('epoch', 'reset', base=105)])
        self.assertEqual(build(self.db)[0].epochs['reset']['rev'], 3)

    def test_recompute_matches_persisted_evidence_from_new_connection(self):
        with closing(connect(self.path)) as other, transaction(other, write=False):
            self.assertEqual(recompute(other), self.sample)
            with self.assertRaises(sqlite3.OperationalError):
                other.execute('SELECT * FROM temporary_results')
        self.assertEqual(demo(self.db, FIXTURE), self.sample)
