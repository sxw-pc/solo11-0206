"""Validate and index an intact export from one physical archive cartridge."""

from hashlib import sha256


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def verified_blocks(packet):
    if not isinstance(packet, dict) or set(packet) != {'id', 'rev', 'cartridge', 'run', 'blocks'}:
        raise ValueError('invalid cartridge header')
    if not all(nonempty(packet[key]) for key in ('id', 'cartridge', 'run')):
        raise ValueError('missing cartridge or press-run identifier')
    if type(packet['rev']) is not int or packet['rev'] < 1:
        raise ValueError('invalid cartridge capture revision')
    if not isinstance(packet['blocks'], list) or not packet['blocks']:
        raise ValueError('at least one block is required')
    found = {}
    for block in packet['blocks']:
        if not isinstance(block, dict) or set(block) != {'id', 'hex', 'sha256'}:
            raise ValueError('invalid block record')
        block_id = block['id']
        if not nonempty(block_id) or block_id in found:
            raise ValueError('missing or duplicate archive block identifier')
        if not nonempty(block['hex']):
            raise ValueError(f'empty archive data: {block_id}')
        try:
            value = bytes.fromhex(block['hex'])
        except ValueError as exc:
            raise ValueError(f'invalid hexadecimal data: {block_id}') from exc
        if sha256(value).hexdigest() != block['sha256']:
            raise ValueError(f'checksum mismatch: {block_id}')
        found[block_id] = value
    return found
