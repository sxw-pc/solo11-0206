import json
from itertools import combinations

from .geometry import area, complement, intersect, intervals, tiles
from .store import current, digest, transaction


def _spans(length, drops):
    points = (0, *drops, length)
    return tuple(zip(points, points[1:]))


def _trial_request(request):
    if not isinstance(request, dict):
        raise ValueError('trial request must be an object')
    missing = {'reel', 'lanes', 'points', 'min_gap', 'max_drops', 'orders'} - set(request)
    if missing:
        raise ValueError('trial request missing: ' + ', '.join(sorted(missing)))
    reel = request['reel']
    if not isinstance(reel, str) or not reel.strip():
        raise ValueError('reel: nonempty text required')
    lanes = request['lanes']
    if (not isinstance(lanes, list)
            or any(not isinstance(lane, str) or not lane.strip() for lane in lanes)):
        raise ValueError('lanes: lane ids required')
    if len(lanes) > 6:
        raise ValueError('at most six lanes')
    if len(set(lanes)) != len(lanes):
        raise ValueError('duplicate lane')
    points = request['points']
    if (not isinstance(points, list)
            or any(type(point) is not int or point <= 0 for point in points)):
        raise ValueError('points: positive integer knife points required')
    if len(points) > 12:
        raise ValueError('at most twelve internal knife points')
    if len(set(points)) != len(points):
        raise ValueError('duplicate knife point')
    min_gap, max_drops = request['min_gap'], request['max_drops']
    if type(min_gap) is not int or type(max_drops) is not int or min_gap < 0 or max_drops < 0:
        raise ValueError('min_gap and max_drops: nonnegative integers required')
    orders = request['orders']
    if not isinstance(orders, list):
        raise ValueError('orders: list required')
    if len(orders) > 6:
        raise ValueError('at most six orders')
    normalized = []
    for order in orders:
        if not isinstance(order, dict):
            raise ValueError('each order must be an object')
        lengths = order.get('lengths')
        if type(lengths) is int:
            lengths = [lengths]
        if (not isinstance(order.get('id'), str) or not order['id'].strip()
                or not isinstance(order.get('lane'), str) or not order['lane'].strip()
                or not isinstance(lengths, list) or not lengths
                or any(type(length) is not int or length <= 0 for length in lengths)):
            raise ValueError('order: id, lane and positive integer lengths required')
        normalized.append({'id': order['id'], 'lane': order['lane'],
                           'lengths': tuple(sorted(set(lengths)))})
    if len({order['id'] for order in normalized}) != len(normalized):
        raise ValueError('duplicate order')
    return {'reel': reel, 'lanes': lanes, 'points': sorted(points),
            'min_gap': min_gap, 'max_drops': max_drops, 'orders': normalized}


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

    def trial(self, request):
        """Schedule one shared-knife sheeter pass over a parent reel.

        One drop cuts every mounted lane, so a single drop sequence is
        checked against the spacing and count limits and every order is
        matched to one clean, scan-covered segment between adjacent drops
        on its own lane. Read-only: returns the cutting sheet, or the
        smallest set of orders that cannot share one pass plus a workable
        sheet for each single deferral from that set.
        """
        request = _trial_request(request)
        reel_id = request['reel']
        reel = self.machine.reels.get(reel_id)
        if reel is None:
            return {'status': 'blocked', 'reel': reel_id, 'reasons': ['missing-reel']}
        machine = self.machine.assess(reel_id)
        box = machine['box']
        if box is None:
            return {'status': 'blocked', 'reel': reel_id, 'reasons': ['unlocated-reel']}
        reasons = []
        if machine['conflicts']:
            reasons.append('reel-conflict')
        lanes = {}
        for lane_id in request['lanes']:
            lane = self.lanes.get(lane_id)
            if lane is None:
                reasons.append('missing-lane:' + lane_id)
                continue
            if lane['reel'] != reel_id:
                reasons.append('lane-reel-mismatch:' + lane_id)
                continue
            if lane['x1'] > reel['width']:
                reasons.append('knife-outside-web:' + lane_id)
            lanes[lane_id] = lane
        for first, second in combinations(sorted(lanes), 2):
            one, other = lanes[first], lanes[second]
            if max(one['x0'], other['x0']) < min(one['x1'], other['x1']):
                reasons.append('knife-conflict:' + first + ':' + second)
        if reasons:
            return {'status': 'blocked', 'reel': reel_id, 'reasons': sorted(reasons)}
        length = box[1] - box[0]
        if any(point >= length for point in request['points']):
            raise ValueError('knife point outside reel')
        incidents, global_holds = [], []
        for incident in machine['incidents']:
            if incident['span'] is None:
                global_holds.append('unmapped-incident:' + incident['id'])
            else:
                incidents.append(incident)

        def holds(lane, lo, hi):
            segment = (box[0] + lo, box[0] + hi, lane['x0'], lane['x1'])
            found = list(global_holds)
            found.extend(incident['kind'] + ':' + incident['id'] for incident in incidents
                         if max(incident['span'][0], segment[0])
                         < min(incident['span'][1], segment[1]))
            if self.inspection.coverage(segment)['missing']:
                found.append('scan-gap')
            for hit in self.inspection.hits:
                hit_box = self.inspection.hit_box(hit)
                if hit_box and hit['grade'] == 'reject' and intersect(hit_box, segment):
                    found.append('reject:' + hit['id'])
            found.extend('unverified-hit:' + hit_id
                         for hit_id in self.inspection.unresolved_rejects(segment))
            return sorted(found)

        cache = {}

        def segment_holds(lane_id, lo, hi):
            key = (lane_id, lo, hi)
            if key not in cache:
                cache[key] = holds(lanes[lane_id], lo, hi)
            return cache[key]

        sequences = []
        for mask in range(1 << len(request['points'])):
            drops = tuple(point for index, point in enumerate(request['points'])
                          if mask & (1 << index))
            if len(drops) <= request['max_drops'] and all(
                    hi - lo >= request['min_gap'] for lo, hi in zip(drops, drops[1:])):
                sequences.append(drops)
        sequences.sort(key=lambda drops: (len(drops), drops))
        wanted = sorted(request['orders'], key=lambda order: order['id'])
        options = {}

        def candidates(drops, order):
            key = (drops, order['id'])
            if key not in options:
                lane_id = order['lane']
                options[key] = [(lo, hi) for lo, hi in _spans(length, drops)
                                if lane_id in lanes and hi - lo in order['lengths']
                                and not segment_holds(lane_id, lo, hi)]
            return options[key]

        def match(drops, subset):
            choice = {}
            used = set()

            def place(index):
                if index == len(subset):
                    return True
                order = subset[index]
                for lo, hi in candidates(drops, order):
                    key = (order['lane'], lo, hi)
                    if key not in used:
                        used.add(key)
                        choice[order['id']] = (lo, hi)
                        if place(index + 1):
                            return True
                        used.discard(key)
                        del choice[order['id']]
                return False

            if any(not candidates(drops, order) for order in subset):
                return None
            return choice if place(0) else None

        def schedule(subset):
            for drops in sequences:
                assignment = match(drops, subset)
                if assignment is not None:
                    return drops, assignment
            return None

        def sheet(drops, assignment):
            served = [order for order in wanted if order['id'] in assignment]
            segments = [{'lane': lane_id, 'lo': lo, 'hi': hi,
                         'order': next((order['id'] for order in served
                                        if order['lane'] == lane_id
                                        and assignment[order['id']] == (lo, hi)), None),
                         'reasons': segment_holds(lane_id, lo, hi)}
                        for lane_id in sorted(lanes) for lo, hi in _spans(length, drops)]
            return {'drops': list(drops),
                    'assignments': [{'order': order['id'], 'lane': order['lane'],
                                     'lo': assignment[order['id']][0],
                                     'hi': assignment[order['id']][1]} for order in served],
                    'segments': segments}

        planned = schedule(wanted)
        if planned is not None:
            drops, assignment = planned
            return {'status': 'scheduled', 'reel': reel_id, **sheet(drops, assignment)}

        def schedulable(ids):
            subset = [order for order in wanted if order['id'] in ids]
            return any(match(drops, subset) is not None for drops in sequences)

        group = []
        for size in range(1, len(wanted) + 1):
            for combo in combinations([order['id'] for order in wanted], size):
                if not schedulable(set(combo)):
                    group = list(combo)
                    break
            if group:
                break
        detail = []
        for order_id in group:
            order = next(order for order in wanted if order['id'] == order_id)
            if order['lane'] not in lanes:
                member = ['lane-not-mounted']
            else:
                blockers, seen = None, False
                for drops in sequences:
                    for lo, hi in _spans(length, drops):
                        if hi - lo in order['lengths']:
                            seen = True
                            segment = set(segment_holds(order['lane'], lo, hi))
                            blockers = segment if blockers is None else blockers & segment
                member = sorted(blockers) if seen else ['no-segment']
            detail.append({'order': order_id, 'reasons': member})
        alternatives = []
        for order_id in group:
            rest = [order for order in wanted if order['id'] != order_id]
            planned = schedule(rest)
            deferred = [order_id]
            if planned is None:
                rest_ids = [order['id'] for order in rest]
                for size in range(len(rest_ids) - 1, -1, -1):
                    for combo in combinations(rest_ids, size):
                        planned = schedule([order for order in wanted if order['id'] in combo])
                        if planned is not None:
                            break
                    if planned is not None:
                        deferred = sorted([order_id, *(set(rest_ids) - set(combo))])
                        break
            drops, assignment = planned
            alternatives.append({'deferred': deferred, **sheet(drops, assignment)})
        return {'status': 'infeasible', 'reel': reel_id, 'group': group,
                'detail': detail, 'alternatives': alternatives}


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
