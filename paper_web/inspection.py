from .geometry import area, intersect, tiles
from .store import current


class Inspection:
    def __init__(self, db, machine):
        self.machine = machine
        self.scans = {row['id']: row for row in current(db, 'scan')}
        self.hits = current(db, 'hit')

    def scan_box(self, scan):
        span = self.machine.span(scan)
        if span and scan['verified']:
            return *span, scan['x0'], scan['x1']
        return None

    def hit_box(self, hit):
        scan = self.scans.get(hit['scan'])
        if not scan:
            return None
        box = self.scan_box(scan)
        span = self.machine.span({**hit, 'epoch': scan['epoch']})
        if box and span:
            candidate = (*span, hit['x0'], hit['x1'])
            if intersect(candidate, box) == candidate:
                return candidate
        return None

    def coverage(self, box):
        windows = []
        for scan in self.scans.values():
            region = self.scan_box(scan)
            clip = intersect(box, region) if region else None
            if clip:
                windows.append((clip, scan['id']))
        covered = sum(area(cell) for cell, _ in tiles(windows))
        return {'covered': covered, 'missing': area(box) - covered,
                'windows': sorted({label for _, label in windows})}

    def unresolved_rejects(self, box):
        unresolved = []
        for hit in self.hits:
            if hit['grade'] != 'reject' or self.hit_box(hit) is not None:
                continue
            scan = self.scans.get(hit['scan'])
            if scan:
                span = self.machine.span({**hit, 'epoch': scan['epoch']})
                if span and not intersect(box, (*span, hit['x0'], hit['x1'])):
                    continue
            unresolved.append(hit['id'])
        return unresolved

    def assess(self, defect):
        hits = [hit for hit in self.hits if hit['defect'] == defect]
        boxes = [(self.hit_box(hit), hit['id']) for hit in hits]
        valid = [(box, label) for box, label in boxes if box]
        projected = {}
        for reel_id, reel in self.machine.reels.items():
            reel_box = self.machine.reel_box(reel)
            if reel_box is None:
                continue
            clips = [(clip, label) for box, label in valid if (clip := intersect(box, reel_box))]
            if clips:
                projected[reel_id] = {'area': sum(area(cell) for cell, _ in tiles(clips)),
                                      'hits': [label for _, label in clips]}
        return {'defect': defect, 'unverified_hits': [label for box, label in boxes if not box],
                'area': sum(area(cell) for cell, _ in tiles(valid)), 'reels': projected}
