import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from paper_web.__main__ import build, demo, parser, recompute, run
from paper_web.claim_receipts import Receipts, receive, settle
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


class ReceiptTests(unittest.TestCase):
    """After-sales credit/refund receipts and signed allocation ledgers."""

    A = 'claim-A'
    B = 'claim-B'

    def _new_database(self):
        path = Path(self.tmp) / 'test.sqlite'
        with closing(connect(path, create=True)) as db:
            demo(db, FIXTURE)
            ingest(db, [
                {'type': 'hit', 'id': 'hA', 'rev': 1, 'scan': 'left-camera', 'defect': 'D-cross',
                 'lo': 1010, 'hi': 1040, 'x0': 5, 'x1': 25, 'grade': 'observe'},
                {'type': 'hit', 'id': 'hB', 'rev': 1, 'scan': 'reset-full', 'defect': 'D-cross',
                 'lo': 8, 'hi': 22, 'x0': 5, 'x1': 25, 'grade': 'observe'},
                {'type': 'claim', 'id': self.A, 'rev': 1, 'defect': 'D-cross', 'cents': 10},
                {'type': 'sample', 'id': 'sA', 'rev': 1, 'claim': self.A, 'epoch': 'original',
                 'lo': 1010, 'hi': 1040, 'x0': 5, 'x1': 25, 'verified': 1},
                {'type': 'claim', 'id': self.B, 'rev': 1, 'defect': 'D-cross', 'cents': 10},
                {'type': 'sample', 'id': 'sB', 'rev': 1, 'claim': self.B, 'epoch': 'reset',
                 'lo': 8, 'hi': 22, 'x0': 5, 'x1': 25, 'verified': 1},
            ])
            finish = build(db)[2]
            for lane in ('A', 'C'):
                decision = finish.assess(lane)
                issue(db, build, lane, decision['basis'], 'after-sales')
            claims = build(db)[3]
            self.assertEqual(claims.assess(self.A)['allocation'], {'child:A': 10})
            self.assertEqual(claims.assess(self.B)['allocation'], {'child:C': 10})
        return path

    def setUp(self):
        self.tmp = self.enterContext(TemporaryDirectory(dir=ROOT))
        self.path = self._new_database()
        self.db = self.enterContext(closing(connect(self.path)))

    def receipts(self):
        return Receipts(self.db, build(self.db)[3])

    def credit_doc(self, cid, cents, claims, rev=1):
        return {'type': 'credit', 'id': cid, 'rev': rev, 'cents': cents, 'claims': claims}

    def refund_doc(self, rid, cents, credit, claim, rev=1, credit_rev=1):
        return {'type': 'refund', 'id': rid, 'rev': rev, 'cents': cents,
                'credit': credit, 'credit_rev': credit_rev, 'claim': claim}

    # ------------------------------------------------------------- validation
    def test_credit_allocation_constraints_and_bound(self):
        receive(self.db, [self.credit_doc('CR-1', 6, [self.A, self.B])])
        view = self.receipts().assess('CR-1')
        self.assertEqual(view['status'], 'pending')
        self.assertEqual(view['bounds'], {self.A: 10, self.B: 10})
        self.assertEqual(view['cents'], 6)
        normalized, reasons = self.receipts().check('CR-1', {self.A: 11, self.B: 0})
        self.assertIn('allocation-total', reasons)
        self.assertIn('bound-exceeded:' + self.A, reasons)
        for bad in ({'unlisted': 6},
                    {self.A: 6, self.B: 0, 'unlisted': 0}):
            _, reasons = self.receipts().check('CR-1', bad)
            self.assertTrue(reasons, bad)
        _, missing = self.receipts().check('CR-1', {self.A: 6})
        self.assertEqual(missing, ['missing-allocation:' + self.B])
        _, short = self.receipts().check('CR-1', {self.A: 5, self.B: 0})
        self.assertIn('allocation-total', short)
        with self.assertRaisesRegex(ValueError, 'receipt held'):
            settle(self.db, build, 'CR-1', {self.A: 11, self.B: 0}, 'op')
        with self.assertRaisesRegex(ValueError, 'bound-exceeded'):
            settle(self.db, build, 'CR-1', {self.A: 11, self.B: 0}, 'op')
        with self.assertRaisesRegex(ValueError, 'nonnegative integer cents'):
            settle(self.db, build, 'CR-1', {self.A: 7, self.B: -1}, 'op')
        # held (reserve) shares cannot fill the confirmed bound
        receive(self.db, [self.credit_doc('CR-9', 963, ['claim-7'])])
        with self.assertRaisesRegex(ValueError, 'bound-exceeded:claim-7'):
            settle(self.db, build, 'CR-9', {'claim-7': 963}, 'op')

    def test_receipt_saves_original_and_rejects_bad_documents(self):
        good = [self.credit_doc('CR-1', 6, [self.A, self.B])]
        self.assertEqual(receive(self.db, good), {'inserted': 1, 'replayed': 0})
        self.assertEqual(receive(self.db, good), {'inserted': 0, 'replayed': 1})
        conflict = [self.credit_doc('CR-1', 5, [self.A, self.B])]
        with self.assertRaisesRegex(ValueError, 'conflicting credit CR-1'):
            receive(self.db, conflict)
        bad = [
            {'type': 'credit', 'id': 'CR-x', 'rev': 1, 'cents': 0, 'claims': [self.A]},
            {'type': 'credit', 'id': 'CR-y', 'rev': 1, 'cents': 4, 'claims': []},
            {'type': 'refund', 'id': 'RF-x', 'rev': 1, 'cents': 2, 'credit': 'CR-1', 'claim': self.A},
        ]
        with self.assertRaises(ValueError):
            receive(self.db, bad)
        self.assertIsNone(self.db.execute(
            "SELECT 1 FROM refund_doc WHERE id='RF-x'").fetchone())

    # --------------------------------------------------------- the A/B scenario
    def post_ab_scenario(self):
        receive(self.db, [
            self.credit_doc('CR-1', 6, [self.A, self.B]),
            self.refund_doc('RF-1', 2, 'CR-1', self.A),
        ])
        view = self.receipts().assess('CR-1')
        self.assertEqual(view['reserve'], {self.A: 2})
        pending = view['pending_refunds'][0]
        self.assertEqual(pending, {'receipt': 'RF-1', 'rev': 1, 'claim': self.A, 'cents': 2})
        with self.assertRaisesRegex(ValueError, 'refund-reserve:' + self.A):
            settle(self.db, build, 'CR-1', {self.A: 1, self.B: 5}, 'after-sales')
        return view

    def test_ab_scenario_option_one_all_to_a(self):
        view = self.post_ab_scenario()
        result = settle(self.db, build, 'CR-1', {self.A: 6, self.B: 0}, 'after-sales')
        self.assertFalse(result['replayed'])
        self.assertEqual(result['net'], {self.A: 4, self.B: 0})
        self.assertEqual(len(result['entries']), 2)
        credit_entry, refund_entry = result['entries']
        self.assertEqual(credit_entry['kind'], 'credit')
        self.assertEqual(refund_entry, {'seq': refund_entry['seq'], 'kind': 'refund',
                                        'receipt': 'RF-1', 'rev': 1,
                                        'witness': refund_entry['witness']})
        ledger = self.receipts().overview()['ledger']
        self.assertEqual(ledger['claims'][self.A],
                         {'bound_cents': 10, 'credit_cents': 6, 'refund_cents': 2,
                          'net_cents': 4, 'available_cents': 4, 'excess_cents': 0})
        self.assertEqual(ledger['claims'][self.B],
                         {'bound_cents': 10, 'credit_cents': 0, 'refund_cents': 0,
                          'net_cents': 0, 'available_cents': 10, 'excess_cents': 0})
        self.assertEqual(ledger['credits']['CR-1']['net_cents'],
                         {self.A: 4, self.B: 0})
        # complete signable witness: details carry allocation, refunds and bounds
        rows = self.db.execute('SELECT detail,witness FROM receipt_entry ORDER BY seq').fetchall()
        detail = json.loads(rows[0]['detail'])
        from paper_web.store import digest
        self.assertEqual(rows[0]['witness'], digest(detail))
        self.assertEqual(detail['allocation'], {self.A: 6, self.B: 0})
        self.assertEqual(detail['bounds'], {self.A: 10, self.B: 10})
        self.assertEqual(detail['refunds'],
                         [{'receipt': 'RF-1', 'rev': 1, 'claim': self.A, 'cents': 2}])
        self.assertEqual([line['net_cents'] for line in detail['lines']], [4, 0])
        refund_detail = json.loads(rows[1]['detail'])
        self.assertEqual((refund_detail['share_cents'], refund_detail['booked_cents'],
                          refund_detail['cumulative_cents']), (6, 0, 2))
        self.assertEqual(view['receipt'], 'CR-1')

    def test_ab_scenario_option_two_split_two_and_four(self):
        self.post_ab_scenario()
        # option two is legal only because A keeps exactly the reserved 2 cents
        result = settle(self.db, build, 'CR-1', {self.A: 2, self.B: 4}, 'after-sales')
        self.assertEqual(result['net'], {self.A: 0, self.B: 4})
        self.assertEqual(self.receipts().assess('RF-1')['status'], 'signed')
        ledger = self.receipts().overview()['ledger']
        self.assertEqual(ledger['credits']['CR-1']['net_cents'],
                         {self.A: 0, self.B: 4})

    def test_refund_is_credit_specific_and_cumulative_cap_holds(self):
        self.post_ab_scenario()
        settle(self.db, build, 'CR-1', {self.A: 6, self.B: 0}, 'after-sales')
        # an independent credit may also pay A up to the remaining confirmed bound,
        # but it cannot cover CR-1 refunds
        receive(self.db, [self.credit_doc('CR-2', 4, [self.A])])
        settle(self.db, build, 'CR-2', {self.A: 4}, 'after-sales')
        receive(self.db, [
            self.refund_doc('RF-2', 4, 'CR-1', self.A),
            self.refund_doc('RF-3', 1, 'CR-1', self.A),
        ])
        view = self.receipts().assess('RF-2')
        self.assertEqual(view['share_cents'], 6)
        self.assertEqual(view['booked_refund_cents'], 2)
        self.assertEqual(view['available_cents'], 4)
        settle(self.db, build, 'RF-2', {}, 'after-sales')
        view = self.receipts().assess('RF-3')
        self.assertIn('credit-share-exceeded:CR-1/' + self.A, view['reasons'])
        with self.assertRaisesRegex(ValueError, 'landed 6 cents'):
            settle(self.db, build, 'RF-3', {}, 'after-sales')
        # other credits cannot be named to absorb the refund: CR-2 has its own share
        receive(self.db, [self.refund_doc('RF-4', 2, 'CR-2', self.A)])
        settle(self.db, build, 'RF-4', {}, 'after-sales')
        self.assertEqual(
            self.receipts().overview()['ledger']['claims'][self.A]['refund_cents'], 8)
        with self.assertRaisesRegex(ValueError, 'CR-1 landed 6 cents.*1 requested'):
            # even after CR-2 exists, RF-3 still cannot be paid from it
            settle(self.db, build, 'RF-3', {}, 'after-sales')

    # ---------------------------------------------------------- revision rules
    def test_authorization_revision_locks_history_and_refund_stays_on_named_credit(self):
        receive(self.db, [
            self.credit_doc('CR-1', 6, [self.A, self.B]),
            self.refund_doc('RF-1', 2, 'CR-1', self.A),
        ])
        settle(self.db, build, 'CR-1', {self.A: 6, self.B: 0}, 'after-sales')
        # independent credit allowed to A, then original credit narrowed to B only
        receive(self.db, [
            self.credit_doc('CR-2', 4, [self.A]),
            self.credit_doc('CR-1', 6, [self.B], rev=2),
        ])
        settle(self.db, build, 'CR-2', {self.A: 4}, 'after-sales')
        revised = self.receipts().assess('CR-1')
        self.assertEqual(revised['rev'], 2)
        self.assertEqual(revised['status'], 'stale')
        self.assertIn('authorization-narrowed:' + self.A, revised['reasons'])
        old = revised['entries'][0]
        self.assertEqual(old['allocation'], {self.A: 6, self.B: 0})
        # new credit cannot underwrite a refund naming the original one
        receive(self.db, [self.refund_doc('RF-2', 2, 'CR-1', self.A, credit_rev=1)])
        view = self.receipts().assess('RF-2')
        self.assertEqual(view['share_cents'], 6)
        self.assertEqual(view['booked_refund_cents'], 2)
        settle(self.db, build, 'RF-2', {}, 'after-sales')
        receive(self.db, [self.refund_doc('RF-3', 3, 'CR-1', self.A, credit_rev=1)])
        with self.assertRaisesRegex(ValueError, 'CR-1 landed 6 cents.*3 requested'):
            settle(self.db, build, 'RF-3', {}, 'after-sales')
        # the narrowed revision cannot give A anything; B remains the only target
        with self.assertRaisesRegex(ValueError, 'outside-authorization:' + self.A):
            settle(self.db, build, 'CR-1', {self.A: 2, self.B: 4}, 'after-sales')
        settle(self.db, build, 'CR-1', {self.B: 6}, 'after-sales')
        self.assertEqual(len(self.receipts().assess('CR-1')['entries']), 2)

    def test_signed_revision_is_append_only_and_replays_idempotently(self):
        receive(self.db, [self.credit_doc('CR-1', 6, [self.A, self.B])])
        result = settle(self.db, build, 'CR-1', {self.A: 6, self.B: 0}, 'after-sales')
        replay = settle(self.db, build, 'CR-1', {self.A: 6, self.B: 0}, 'after-sales')
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['entries'], result['entries'])
        with self.assertRaisesRegex(ValueError, 'locked'):
            settle(self.db, build, 'CR-1', {self.A: 0, self.B: 6}, 'after-sales')
        receive(self.db, [self.refund_doc('RF-1', 2, 'CR-1', self.A)])
        once = settle(self.db, build, 'RF-1', {}, 'after-sales')
        again = settle(self.db, build, 'RF-1', {}, 'after-sales')
        self.assertTrue(again['replayed'])
        self.assertEqual(again['entries'], once['entries'])
        # old entry remains queryable after a new revision arrives
        receive(self.db, [self.credit_doc('CR-1', 6, [self.A, self.B], rev=2)])
        self.assertEqual(self.receipts().assess('CR-1')['entries'][0]['rev'], 1)

    def test_refund_requires_signed_named_credit_revision(self):
        # refund arriving before its credit is posted cannot be signed yet
        receive(self.db, [
            self.credit_doc('CR-1', 6, [self.A, self.B]),
            self.refund_doc('RF-early', 2, 'CR-1', self.A),
        ])
        view = self.receipts().assess('RF-early')
        self.assertIn('credit-not-posted:CR-1', view['reasons'])
        with self.assertRaisesRegex(ValueError, 'post the credit first'):
            settle(self.db, build, 'RF-early', {}, 'after-sales')
        # a refund naming a revision that never existed is rejected
        receive(self.db, [self.refund_doc('RF-ghost', 1, 'CR-1', self.A,
                                          rev=2, credit_rev=9)])
        ghost = self.receipts().assess('RF-ghost')
        self.assertIn('unknown-credit:CR-1@9', ghost['reasons'])
        with self.assertRaisesRegex(ValueError, 'unknown credit CR-1@9'):
            settle(self.db, build, 'RF-ghost', {}, 'after-sales')

    def test_late_refund_on_old_revision_does_not_block_new_revision_posting(self):
        receive(self.db, [self.credit_doc('CR-1', 6, [self.A, self.B])])
        settle(self.db, build, 'CR-1', {self.A: 6, self.B: 0}, 'after-sales')
        # the credit's authorization is narrowed; a new refund naming rev1 arrives later
        receive(self.db, [
            self.credit_doc('CR-1', 6, [self.B], rev=2),
            self.refund_doc('RF-late', 1, 'CR-1', self.A, rev=2, credit_rev=1),
        ])
        revised = self.receipts().assess('CR-1')
        self.assertEqual(revised['reserve'], {})
        self.assertEqual(revised['pending_refunds'], [])
        # rev2 posts cleanly, RF-late draws on the rev1 landed share afterwards
        settle(self.db, build, 'CR-1', {self.B: 6}, 'after-sales')
        late = self.receipts().assess('RF-late')
        self.assertEqual(late['share_cents'], 6)
        self.assertEqual(late['available_cents'], 6)
        settle(self.db, build, 'RF-late', {}, 'after-sales')
        ledger = self.receipts().overview()['ledger']
        self.assertEqual(ledger['credits']['CR-1']['net_cents'],
                         {self.A: 5, self.B: 6})

    # ------------------------------------------------------- evidence correction
    def test_evidence_correction_reports_discrepancy_without_rewriting_history(self):
        receive(self.db, [
            self.credit_doc('CR-1', 6, [self.A, self.B]),
            self.refund_doc('RF-1', 2, 'CR-1', self.A),
        ])
        settle(self.db, build, 'CR-1', {self.A: 6, self.B: 0}, 'after-sales')
        # corrected evidence invalidates the A sub-roll signature: bound drops 10 -> 0
        ingest(self.db, json.loads((ROOT / 'fixtures/correction.json').read_text(
            encoding='utf-8-sig')))
        overview = self.receipts().overview()
        self.assertEqual(overview['discrepancies'],
                         [{'claim': self.A, 'signed_cents': 6, 'bound_cents': 0,
                           'excess_cents': 6}])
        claim = overview['ledger']['claims'][self.A]
        self.assertEqual(claim['credit_cents'], 6)
        self.assertEqual(claim['refund_cents'], 2)
        self.assertEqual(claim['net_cents'], 4)
        self.assertEqual(claim['available_cents'], 0)
        # signed details remain fully queryable, and new receipts re-judge on current bounds
        self.assertEqual(self.receipts().assess('CR-1')['entries'][0]['rev'], 1)
        receive(self.db, [self.credit_doc('CR-2', 4, [self.A])])
        with self.assertRaisesRegex(ValueError, 'bound-exceeded:' + self.A):
            settle(self.db, build, 'CR-2', {self.A: 4}, 'after-sales')
        # restoring the evidence restores the bound; the old discrepancy clears
        ingest(self.db, [
            {'type': 'scan', 'id': 'left-camera', 'rev': 3, 'epoch': 'original',
             'lo': 1000, 'hi': 1120, 'x0': 0, 'x1': 100, 'verified': 1},
        ])
        finish = build(self.db)[2]
        issue(self.db, build, 'A', finish.assess('A')['basis'], 'recovered')
        self.assertEqual(self.receipts().overview()['discrepancies'], [])
        settle(self.db, build, 'CR-2', {self.A: 4}, 'after-sales')
        self.assertEqual(self.receipts().overview()['ledger']['claims'][self.A],
                         {'bound_cents': 10, 'credit_cents': 10, 'refund_cents': 2,
                          'net_cents': 8, 'available_cents': 0, 'excess_cents': 0})

    # ------------------------------------------------------------------ CLI
    def test_cli_receipts_receipt_settle_and_ledger(self):
        receipt_file = Path(self.tmp) / 'receipts.json'
        receipt_file.write_text(json.dumps([
            self.credit_doc('CR-1', 6, [self.A, self.B]),
            self.refund_doc('RF-1', 2, 'CR-1', self.A),
        ]), encoding='utf-8')
        cli = parser()
        saved = run(self.db, cli.parse_args(
            ['--db', str(self.path), 'receipts', str(receipt_file)]))
        self.assertEqual(saved, {'inserted': 2, 'replayed': 0})
        view = run(self.db, cli.parse_args(['--db', str(self.path), 'receipt', 'CR-1']))
        self.assertEqual(view['reserve'], {self.A: 2})
        posted = run(self.db, cli.parse_args([
            '--db', str(self.path), 'settle', 'CR-1',
            json.dumps({self.A: 6, self.B: 0}), 'after-sales']))
        self.assertEqual(posted['net'], {self.A: 4, self.B: 0})
        ledger = run(self.db, cli.parse_args(['--db', str(self.path), 'ledger']))
        self.assertEqual(ledger['ledger']['credits']['CR-1']['net_cents'],
                         {self.A: 4, self.B: 0})
        rf = run(self.db, cli.parse_args(['--db', str(self.path), 'receipt', 'RF-1']))
        self.assertEqual(rf['status'], 'signed')
        with self.assertRaises(sqlite3.OperationalError):
            self.db.execute('SELECT * FROM no_such_table')
