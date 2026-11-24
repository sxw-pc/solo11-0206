"""Accept closed packets from a dedicated waste-water sampler."""

import json

from .store import canonical, current, ingest
from .water_samples import bottle_totals


def receive(db, packet):
    totals = bottle_totals(packet)
    stored = ingest(db, [{
        'type': 'water_packet',
        'id': packet['id'],
        'rev': packet['rev'],
        'packet': canonical(packet),
    }])
    return stored | {'bottles': totals}


def report(db, packet_id):
    row = next((row for row in current(db, 'water_packet') if row['id'] == packet_id), None)
    if row is None:
        raise ValueError(f'unknown sampler packet: {packet_id}')
    return {'id': packet_id, 'rev': row['rev'], 'bottles': bottle_totals(json.loads(row['packet']))}
