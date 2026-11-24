"""Persist and replay a microscope trace export and its current measurement."""

import json

from .fibre_measure import measured_traces
from .store import canonical, current, ingest


def receive(db, image):
    histogram = measured_traces(image)
    saved = ingest(db, [{'type': 'fibre_image', 'id': image['id'], 'rev': image['rev'],
                         'image': canonical(image)}])
    return saved | {'tray': image['tray'], 'histogram': histogram}


def report(db, image_id):
    row = next((row for row in current(db, 'fibre_image') if row['id'] == image_id), None)
    if row is None:
        raise ValueError(f'unknown fibre image: {image_id}')
    image = json.loads(row['image'])
    return {'id': image_id, 'rev': row['rev'], 'tray': image['tray'], 'histogram': measured_traces(image)}
