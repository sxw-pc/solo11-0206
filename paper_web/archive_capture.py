"""Record cartridge reads and restore a block from its archived original."""

import json

from .archive_blocks import verified_blocks
from .store import canonical, current, ingest


def receive(db, packet):
    blocks = verified_blocks(packet)
    result = ingest(db, [{'type': 'cartridge_read', 'id': packet['id'], 'rev': packet['rev'],
                          'packet': canonical(packet)}])
    return result | {'cartridge': packet['cartridge'], 'blocks': sorted(blocks)}


def restore(db, packet_id, block_id):
    row = next((row for row in current(db, 'cartridge_read') if row['id'] == packet_id), None)
    if row is None:
        raise ValueError(f'unknown cartridge read: {packet_id}')
    packet = json.loads(row['packet'])
    blocks = verified_blocks(packet)
    if block_id not in blocks:
        raise ValueError(f'block absent from cartridge: {block_id}')
    return {'read': packet_id, 'rev': row['rev'], 'run': packet['run'],
            'cartridge': packet['cartridge'], 'block': block_id, 'hex': blocks[block_id].hex()}
