"""Measure a settled-fibre tray from a calibrated, non-overlapping trace export."""

from collections import Counter
from math import isqrt


def measured_traces(image):
    if not isinstance(image, dict) or set(image) != {'id', 'rev', 'tray', 'um_per_pixel', 'traces'}:
        raise ValueError('invalid fibre image header')
    if not all(isinstance(image[key], str) and image[key].strip() for key in ('id', 'tray')):
        raise ValueError('image and tray identifiers are required')
    if type(image['rev']) is not int or image['rev'] < 1:
        raise ValueError('invalid image revision')
    scale = image['um_per_pixel']
    if type(scale) is not int or scale < 1:
        raise ValueError('invalid calibration scale')
    if not isinstance(image['traces'], list) or not image['traces']:
        raise ValueError('at least one trace is required')

    lengths = Counter()
    seen = set()
    for trace in image['traces']:
        if not isinstance(trace, dict) or set(trace) != {'id', 'start', 'stop'}:
            raise ValueError('invalid trace')
        trace_id = trace['id']
        if not isinstance(trace_id, str) or not trace_id.strip() or trace_id in seen:
            raise ValueError('duplicate or empty trace identifier')
        for point in (trace['start'], trace['stop']):
            if not isinstance(point, list) or len(point) != 2 or any(type(n) is not int or n < 0 for n in point):
                raise ValueError(f'invalid endpoint: {trace_id}')
        start, stop = trace['start'], trace['stop']
        squared_um = scale * scale * sum((a - b) ** 2 for a, b in zip(start, stop))
        if squared_um == 0:
            raise ValueError(f'empty trace: {trace_id}')
        lengths[isqrt(squared_um)] += 1
        seen.add(trace_id)
    return [{'length_um': length, 'count': count} for length, count in sorted(lengths.items())]
