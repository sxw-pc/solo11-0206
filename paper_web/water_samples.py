"""Derive physical dose volumes from a dedicated-inlet counter."""


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def natural(value):
    return type(value) is int and value >= 0


def bottle_totals(packet):
    require(isinstance(packet, dict) and set(packet) == {
        'id', 'rev', 'sampler', 'inlet', 'ml_per_pulse', 'readings', 'bottles'
    }, 'invalid sampler packet header')
    require(all(text(packet[key]) for key in ('id', 'sampler', 'inlet')), 'missing sampler identity')
    require(natural(packet['rev']) and packet['rev'] > 0, 'invalid sampler revision')
    pulse_ml = packet['ml_per_pulse']
    require(natural(pulse_ml) and pulse_ml > 0, 'invalid pulse volume')
    readings = packet['readings']
    require(isinstance(readings, list) and len(readings) >= 2, 'two counter readings are required')
    counters = {}
    last_time, last_count = -1, 0
    for reading in readings:
        require(isinstance(reading, dict) and set(reading) == {'at', 'count'}, 'invalid counter reading')
        at, count = reading['at'], reading['count']
        require(natural(at) and natural(count) and at > last_time and count >= last_count,
                'counter time or count moved backwards')
        counters[at] = count
        last_time, last_count = at, count

    bottles = packet['bottles']
    require(isinstance(bottles, list) and bottles, 'at least one sealed bottle is required')
    totals = []
    seen = set()
    previous_close = -1
    for bottle in bottles:
        require(isinstance(bottle, dict) and set(bottle) == {'id', 'open', 'close', 'capacity_ml'},
                'invalid bottle record')
        bottle_id, opened, closed, capacity = (bottle[key] for key in ('id', 'open', 'close', 'capacity_ml'))
        require(text(bottle_id) and bottle_id not in seen, 'duplicate or empty bottle identifier')
        require(natural(opened) and natural(closed) and previous_close <= opened < closed,
                f'overlapping or reversed bottle interval: {bottle_id}')
        require(opened in counters and closed in counters, f'missing counter boundary: {bottle_id}')
        volume = (counters[closed] - counters[opened]) * pulse_ml
        require(natural(capacity) and 0 < volume <= capacity, f'bottle volume outside capacity: {bottle_id}')
        totals.append(bottle | {'inlet': packet['inlet'], 'volume_ml': volume,
                                'counter_span': [counters[opened], counters[closed]]})
        previous_close = closed
        seen.add(bottle_id)
    return totals
