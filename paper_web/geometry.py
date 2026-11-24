def intersect(a, b):
    box = (max(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), min(a[3], b[3]))
    return box if box[0] < box[1] and box[2] < box[3] else None


def area(box):
    return (box[1] - box[0]) * (box[3] - box[2])


def tiles(boxes):
    """Yield elementary disjoint rectangles and the input boxes covering each."""
    if not boxes:
        return
    ys = sorted({p for box, _ in boxes for p in box[:2]})
    for lo, hi in zip(ys, ys[1:]):
        strip = [(box, label) for box, label in boxes if box[0] <= lo < box[1]]
        xs = sorted({p for box, _ in strip for p in box[2:]})
        for x0, x1 in zip(xs, xs[1:]):
            labels = [label for box, label in strip if box[2] <= x0 < box[3]]
            if labels:
                yield (lo, hi, x0, x1), labels


def intervals(spans):
    merged = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return merged


def complement(lo, hi, removed):
    start = lo
    for left, right in removed:
        if start < left:
            yield start, left
        start = right
    if start < hi:
        yield start, hi
