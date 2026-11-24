from collections import Counter

from .geometry import area, intersect, tiles
from .store import current


def cents_by_area(cents, areas):
    total = sum(areas.values())
    if not total:
        return {'reserve': cents}
    amounts = {key: cents * value // total for key, value in areas.items()}
    rank = sorted(areas, key=lambda key: (-(cents * areas[key] % total), key))
    for key in rank[:cents - sum(amounts.values())]:
        amounts[key] += 1
    return dict(sorted(amounts.items()))


class Claims:
    def __init__(self, db, machine, inspection, finishing):
        self.claims = {row['id']: row for row in current(db, 'claim')}
        self.samples = current(db, 'sample')
        self.machine = machine
        self.inspection = inspection
        self.finishing = finishing

    def assess(self, claim_id):
        claim = self.claims[claim_id]
        samples = [row for row in self.samples if row['claim'] == claim_id]
        basis = {'claim_revision': claim['rev'], 'samples': [], 'invalid_samples': []}
        windows = []
        for sample in samples:
            span = self.machine.span(sample)
            if span and sample['verified']:
                windows.append(((*span, sample['x0'], sample['x1']), ('sample', sample['id'])))
                basis['samples'].append(sample['id'])
            else:
                basis['invalid_samples'].append(sample['id'])
        if not windows or basis['invalid_samples']:
            return {'claim': claim_id, **basis, 'status': 'pending-sample', 'areas': {},
                    'allocation': {'reserve': claim['cents']}, 'cells': []}
        defects = [hit for hit in self.inspection.hits if hit['defect'] == claim['defect']]
        boxes = [(box, ('defect', hit['id'])) for hit in defects if (box := self.inspection.hit_box(hit))]
        unresolved = any(self.inspection.hit_box(hit) is None for hit in defects)
        for scan in self.inspection.scans.values():
            if (box := self.inspection.scan_box(scan)):
                boxes.append((box, ('scan', scan['id'])))
        for lane in self.finishing.lanes:
            result = self.finishing.assess(lane)
            for box in result['retained']:
                kind = 'signed' if result['signature'] == 'current' else 'held'
                boxes.append((box, (kind, lane)))
            for box in result['removed']:
                kind = 'cut' if not result['evidence']['plan']['reasons'] else 'held'
                boxes.append((box, (kind, lane)))
        regions = windows[:]
        for box, label in boxes:
            regions.extend((clip, label) for window, _ in windows if (clip := intersect(box, window)))
        areas = Counter()
        cells = []
        for cell, raw_labels in tiles(regions):
            labels = sorted(set(raw_labels))
            kinds = Counter(kind for kind, _ in labels)
            if 'sample' not in kinds:
                continue
            disposition = 'reserve'
            ownership = sum(kinds[kind] for kind in ('signed', 'held', 'cut'))
            if kinds['defect'] and ownership == 1 and kinds['signed']:
                disposition = 'child:' + next(lane for kind, lane in labels if kind == 'signed')
            elif kinds['defect'] and ownership == 1 and kinds['cut']:
                disposition = 'removed'
            elif not kinds['defect'] and kinds['scan'] and not unresolved:
                disposition = 'no-confirmed-defect'
            areas[disposition] += area(cell)
            cells.append({'box': cell, 'disposition': disposition, 'evidence': labels})
        return {'claim': claim_id, **basis, 'status': 'partial' if areas['reserve'] else 'reconciled',
                'areas': dict(sorted(areas.items())), 'allocation': cents_by_area(claim['cents'], areas),
                'cells': cells}
