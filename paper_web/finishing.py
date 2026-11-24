import json

from .geometry import area, complement, intersect, intervals, tiles
from .store import current, digest, transaction


class Finishing:
    def __init__(self, db, machine, inspection):
        self.db = db
        self.machine = machine
        self.inspection = inspection
        self.lanes = {row['id']: row for row in current(db, 'lane')}
        self.cuts = current(db, 'cut')
        self.weights = {row['id']: row for row in current(db, 'weight')}
        self.issues = {row['lane']: dict(row) for row in db.execute('SELECT * FROM issue ORDER BY seq')}

    def plan(self, lane_id):
        lane = self.lanes[lane_id]
        reel = self.machine.reels.get(lane['reel'])
        cuts = [cut for cut in self.cuts if cut['lane'] == lane_id]
        reasons = []
        if reel is None:
            return {'reasons': ['missing-reel'], 'removed': [], 'retained': [], 'plan': None}
        reel_result = self.machine.assess(reel['id'])
        box = reel_result['box']
        if box is None:
            return {'reasons': ['unlocated-reel'], 'removed': [], 'retained': [], 'plan': None}
        if reel_result['conflicts']:
            reasons.append('reel-conflict')
        if lane['x1'] > reel['width']:
            reasons.append('knife-outside-web')
        for other in self.lanes.values():
            if other['id'] != lane_id and other['reel'] == lane['reel']:
                if max(lane['x0'], other['x0']) < min(lane['x1'], other['x1']):
                    reasons.append('knife-conflict:' + other['id'])
        y0, y1 = box[:2]
        if any(cut['hi'] > y1 - y0 for cut in cuts):
            reasons.append('cut-outside-reel')
        spans = intervals((y0 + cut['lo'], y0 + cut['hi']) for cut in cuts if cut['hi'] <= y1 - y0)
        removed = [(*span, lane['x0'], lane['x1']) for span in spans]
        retained = [(*span, lane['x0'], lane['x1']) for span in complement(y0, y1, spans)]
        if not retained:
            reasons.append('empty-child')
        key = digest({'lane': lane, 'reel': reel, 'box': box, 'cuts': cuts})
        return {'reasons': reasons, 'removed': removed, 'retained': retained, 'plan': key}

    def assess(self, lane_id):
        lane = self.lanes[lane_id]
        plan = self.plan(lane_id)
        reasons = plan['reasons'][:]
        reel = self.machine.reels.get(lane['reel'])
        machine = self.machine.assess(reel['id']) if reel else None
        if machine:
            for incident in machine['incidents']:
                if incident['span'] is None:
                    reasons.append('unmapped-incident:' + incident['id'])
                elif any(max(incident['span'][0], box[0]) < min(incident['span'][1], box[1])
                         for box in plan['retained']):
                    reasons.append(incident['kind'] + ':' + incident['id'])
        coverage = [self.inspection.coverage(box) for box in plan['retained']]
        missing = sum(region['missing'] for region in coverage)
        if missing:
            reasons.append('scan-gap')
        unresolved = sorted({hit for box in plan['retained'] for hit in self.inspection.unresolved_rejects(box)})
        reasons.extend('unverified-hit:' + hit for hit in unresolved)
        exposed = []
        hit_evidence = []
        removed_hits = []
        for hit in self.inspection.hits:
            box = self.inspection.hit_box(hit)
            if box is None:
                continue
            kept = sum(area(clip) for piece in plan['retained'] if (clip := intersect(box, piece)))
            cut = sum(area(clip) for piece in plan['removed'] if (clip := intersect(box, piece)))
            if kept or cut:
                hit_evidence.append(hit)
            if cut:
                removed_hits.append({'hit': hit['id'], 'area': cut})
            if kept:
                exposed.append({'hit': hit['id'], 'area': kept})
                if hit['grade'] == 'reject':
                    reasons.append('reject:' + hit['id'])
        candidates = [w for w in self.weights.values() if w['lane'] == lane_id and w['plan'] == plan['plan']]
        weight_status = 'missing'
        if len(candidates) > 1:
            weight_status = 'conflicting'
        if len(candidates) == 1:
            weight_status = ('accepted' if abs(candidates[0]['grams'] - lane['target']) <= lane['tolerance']
                             else 'outside-tolerance')
        if weight_status != 'accepted':
            reasons.append('weight-' + weight_status)
        evidence = {'plan': plan, 'machine': machine, 'coverage': coverage,
                    'scans': [self.inspection.scans[key] for key in sorted({
                        key for region in coverage for key in region['windows']
                    } | {hit['scan'] for hit in hit_evidence})],
                    'hits': hit_evidence, 'weights': candidates}
        basis = digest(evidence)
        latest = self.issues.get(lane_id)
        signature = 'unsigned' if latest is None else 'current'
        if latest and (latest['basis'] != basis or reasons):
            signature = 'stale'
        return {'lane': lane_id, **plan, 'reasons': reasons, 'covered_area': sum(r['covered'] for r in coverage),
                'missing_area': missing, 'exposed': exposed, 'removed_hits': removed_hits,
                'weight': weight_status, 'status': 'hold' if reasons else 'eligible', 'basis': basis,
                'signature': signature, 'issue': latest['seq'] if latest else None, 'evidence': evidence}


def weigh(db, lane, ticket, rev, grams, plan):
    from .store import ingest

    return ingest(db, [{'type': 'weight', 'lane': lane, 'id': ticket, 'grams': grams, 'rev': rev, 'plan': plan}])


def issue(db, build, lane, basis, operator):
    if not operator.strip():
        raise ValueError('operator required')
    with transaction(db):
        finishing = build(db)[2]
        decision = finishing.assess(lane)
        if basis != decision['basis']:
            raise ValueError('release basis changed; assess again')
        if decision['status'] != 'eligible':
            raise ValueError('release held: ' + ', '.join(decision['reasons']))
        if decision['signature'] == 'current':
            return decision['issue']
        return db.execute('INSERT INTO issue(lane,basis,operator,evidence) VALUES (?,?,?,?)',
                          (lane, basis, operator, json.dumps(decision, sort_keys=True))).lastrowid
