from .geometry import intersect
from .store import current


def map_span(epochs, epoch_id, lo, hi):
    epoch = epochs.get(epoch_id)
    if epoch is None or not epoch['first'] <= lo < hi <= epoch['last']:
        return None
    offset = epoch['base'] - epoch['first']
    return lo + offset, hi + offset


class Machine:
    def __init__(self, db):
        self.epochs = {row['id']: row for row in current(db, 'epoch')}
        self.reels = {row['id']: row for row in current(db, 'reel')}
        self.incidents = current(db, 'incident')

    def span(self, row):
        return map_span(self.epochs, row['epoch'], row['lo'], row['hi'])

    def reel_box(self, row):
        span = self.span(row)
        return (*span, 0, row['width']) if span else None

    def assess(self, reel_id):
        reel = self.reels[reel_id]
        box = self.reel_box(reel)
        if box is None:
            return {'reel': reel_id, 'status': 'unlocated', 'box': None, 'conflicts': [], 'incidents': []}
        conflicts = []
        for other in self.reels.values():
            other_box = self.reel_box(other)
            if reel_id != other['id'] and other_box and intersect(box, other_box):
                conflicts.append(other['id'])
        impacts = []
        for incident in self.incidents:
            span = self.span(incident)
            if span is None and incident['epoch'] == reel['epoch']:
                impacts.append({'id': incident['id'], 'kind': incident['kind'], 'span': None})
            if span and max(box[0], span[0]) < min(box[1], span[1]):
                impacts.append({'id': incident['id'], 'kind': incident['kind'],
                                'span': (max(box[0], span[0]), min(box[1], span[1]))})
        return {'reel': reel_id, 'status': 'conflict' if conflicts else 'located', 'box': box,
                'conflicts': conflicts, 'incidents': impacts}
