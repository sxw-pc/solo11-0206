import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from paper_web.__main__ import build, demo, parser, recompute, run
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


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(TemporaryDirectory(dir=ROOT))
        self.path = Path(self.tmp) / 'trial.sqlite'
        self.db = self.enterContext(closing(connect(self.path, create=True)))
        ingest(self.db, [
            {'type': 'epoch', 'id': 'e', 'rev': 1, 'base': 0, 'first': 0, 'last': 100},
            {'type': 'reel', 'id': 'W', 'rev': 1, 'epoch': 'e', 'lo': 0, 'hi': 100, 'width': 20},
            {'type': 'lane', 'id': 'L', 'rev': 1, 'reel': 'W', 'x0': 0, 'x1': 10,
             'target': 100, 'tolerance': 5},
            {'type': 'lane', 'id': 'R', 'rev': 1, 'reel': 'W', 'x0': 10, 'x1': 20,
             'target': 100, 'tolerance': 5},
            {'type': 'scan', 'id': 'cam', 'rev': 1, 'epoch': 'e', 'lo': 0, 'hi': 100,
             'x0': 0, 'x1': 20, 'verified': 1},
            {'type': 'hit', 'id': 'mark', 'rev': 1, 'scan': 'cam', 'defect': 'D',
             'lo': 48, 'hi': 52, 'x0': 0, 'x1': 10, 'grade': 'reject'},
        ])
        self.orders = [
            {'id': 'left-order', 'lane': 'L', 'lengths': [48]},
            {'id': 'right-order', 'lane': 'R', 'lengths': [100]},
        ]

    def request(self, orders, **changes):
        base = {'reel': 'W', 'lanes': ['L', 'R'], 'points': [48, 52],
                'min_gap': 1, 'max_drops': 1, 'orders': orders}
        return {**base, **changes}

    def trial(self, request):
        with transaction(self.db, write=False):
            return build(self.db)[2].trial(request)

    def test_pair_cannot_share_one_pass_and_each_deferral_works(self):
        result = self.trial(self.request(self.orders))
        self.assertEqual(result['status'], 'infeasible')
        self.assertEqual(result['group'], ['left-order', 'right-order'])
        alternatives = {tuple(alt['deferred']): alt for alt in result['alternatives']}
        self.assertEqual(set(alternatives), {('left-order',), ('right-order',)})

        drop_at_48 = alternatives[('right-order',)]
        self.assertEqual(drop_at_48['drops'], [48])
        self.assertEqual(drop_at_48['assignments'],
                         [{'order': 'left-order', 'lane': 'L', 'lo': 0, 'hi': 48}])
        left_segments = [seg for seg in drop_at_48['segments'] if seg['lane'] == 'L']
        self.assertEqual(left_segments[1]['reasons'], ['reject:mark'])
        severed = [seg for seg in drop_at_48['segments'] if seg['lane'] == 'R']
        self.assertEqual([(seg['lo'], seg['hi'], seg['order']) for seg in severed],
                         [(0, 48, None), (48, 100, None)])

        whole_web = alternatives[('left-order',)]
        self.assertEqual(whole_web['drops'], [])
        self.assertEqual(whole_web['assignments'],
                         [{'order': 'right-order', 'lane': 'R', 'lo': 0, 'hi': 100}])

    def test_shared_drop_when_right_also_accepts_48(self):
        orders = [self.orders[0], {'id': 'right-order', 'lane': 'R', 'lengths': [100, 48]}]
        result = self.trial(self.request(orders))
        self.assertEqual(result['status'], 'scheduled')
        self.assertEqual(result['drops'], [48])
        self.assertEqual(result['assignments'],
                         [{'order': 'left-order', 'lane': 'L', 'lo': 0, 'hi': 48},
                          {'order': 'right-order', 'lane': 'R', 'lo': 0, 'hi': 48}])

    def test_scan_withdrawal_leaves_orders_without_evidence(self):
        ingest(self.db, [{'type': 'scan', 'id': 'cam', 'rev': 2, 'epoch': 'e',
                          'lo': 0, 'hi': 100, 'x0': 0, 'x1': 20, 'verified': 0}])
        pair = self.trial(self.request(self.orders))
        self.assertEqual(pair['status'], 'infeasible')
        self.assertEqual(len(pair['group']), 1)
        for order in self.orders:
            solo = self.trial(self.request([order]))
            self.assertEqual(solo['status'], 'infeasible')
            self.assertEqual(solo['group'], [order['id']])
            self.assertIn('scan-gap', solo['detail'][0]['reasons'])

    def test_unlocated_reel_and_knife_conflict_report_reasons(self):
        ingest(self.db, [{'type': 'lane', 'id': 'R', 'rev': 2, 'reel': 'W',
                          'x0': 5, 'x1': 15, 'target': 100, 'tolerance': 5}])
        conflict = self.trial(self.request(self.orders))
        self.assertEqual(conflict['status'], 'blocked')
        self.assertIn('knife-conflict:L:R', conflict['reasons'])
        ingest(self.db, [{'type': 'reel', 'id': 'W', 'rev': 2, 'epoch': 'gone',
                          'lo': 0, 'hi': 100, 'width': 20}])
        unlocated = self.trial(self.request(self.orders))
        self.assertEqual(unlocated['status'], 'blocked')
        self.assertEqual(unlocated['reasons'], ['unlocated-reel'])

    def test_spacing_and_count_apply_to_one_shared_sequence(self):
        orders = [{'id': 'o1', 'lane': 'L', 'lengths': [48]},
                  {'id': 'o2', 'lane': 'L', 'lengths': [48]}]
        tight = self.trial(self.request(orders, min_gap=10, max_drops=2))
        self.assertEqual(tight['status'], 'infeasible')
        self.assertEqual(tight['group'], ['o1', 'o2'])
        capped = self.trial(self.request(orders, min_gap=1, max_drops=1))
        self.assertEqual(capped['status'], 'infeasible')
        shared = self.trial(self.request(orders, min_gap=4, max_drops=2))
        self.assertEqual(shared['status'], 'scheduled')
        self.assertEqual(shared['drops'], [48, 52])
        self.assertEqual(shared['assignments'],
                         [{'order': 'o1', 'lane': 'L', 'lo': 0, 'hi': 48},
                          {'order': 'o2', 'lane': 'L', 'lo': 52, 'hi': 100}])

    def test_request_limits(self):
        with self.assertRaisesRegex(ValueError, 'twelve internal knife points'):
            self.trial(self.request([], points=list(range(1, 14))))
        with self.assertRaisesRegex(ValueError, 'at most six lanes'):
            self.trial(self.request([], lanes=['L'] * 7))
        with self.assertRaisesRegex(ValueError, 'at most six orders'):
            self.trial(self.request([{'id': f'o{i}', 'lane': 'L', 'lengths': [48]}
                                     for i in range(7)]))

    def test_cli_trial_entry(self):
        ticket = Path(self.tmp) / 'trial.json'
        ticket.write_text(json.dumps(self.request(self.orders)), encoding='utf-8')
        args = parser().parse_args(['--db', str(self.path), 'trial', str(ticket)])
        result = run(self.db, args)
        self.assertEqual(result['status'], 'infeasible')
        self.assertEqual(result['group'], ['left-order', 'right-order'])
