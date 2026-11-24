import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA = {
    'cartridge_read': 'packet TEXT',
    'epoch': 'base INTEGER, first INTEGER, last INTEGER',
    'reel': 'epoch TEXT, lo INTEGER, hi INTEGER, width INTEGER',
    'incident': 'epoch TEXT, lo INTEGER, hi INTEGER, kind TEXT',
    'scan': 'epoch TEXT, lo INTEGER, hi INTEGER, x0 INTEGER, x1 INTEGER, verified INTEGER',
    'hit': 'scan TEXT, defect TEXT, lo INTEGER, hi INTEGER, x0 INTEGER, x1 INTEGER, grade TEXT',
    'lane': 'reel TEXT, x0 INTEGER, x1 INTEGER, target INTEGER, tolerance INTEGER',
    'cut': 'lane TEXT, lo INTEGER, hi INTEGER',
    'weight': 'lane TEXT, grams INTEGER, plan TEXT',
    'claim': 'defect TEXT, cents INTEGER',
    'sample': 'claim TEXT, epoch TEXT, lo INTEGER, hi INTEGER, x0 INTEGER, x1 INTEGER, verified INTEGER',
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def connect(path, create=False):
    if not create and not Path(path).is_file():
        raise ValueError('database missing; run ingest or demo first')
    db = sqlite3.connect(path, isolation_level=None)
    db.row_factory = sqlite3.Row
    if create:
        with transaction(db):
            for table, fields in SCHEMA.items():
                columns = ', '.join(f'{field} NOT NULL' for field in fields.split(', '))
                db.execute(f'CREATE TABLE IF NOT EXISTS {table} ('
                           f'id TEXT NOT NULL, rev INTEGER NOT NULL, {columns}, PRIMARY KEY(id, rev))')
            db.execute('CREATE TABLE IF NOT EXISTS issue ('
                       'seq INTEGER PRIMARY KEY, lane TEXT NOT NULL, basis TEXT NOT NULL, '
                       'operator TEXT NOT NULL, evidence TEXT NOT NULL)')
    return db


@contextmanager
def transaction(db, write=True):
    db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def current(db, table):
    return [dict(row) for row in db.execute(
        f'SELECT a.* FROM {table} a JOIN (SELECT id, max(rev) AS rev FROM {table} GROUP BY id) b '
        'USING(id, rev) ORDER BY a.id')]


def ingest(db, records):
    if not isinstance(records, list):
        raise ValueError('fixture must contain an array of records')
    added = 0
    with transaction(db):
        for record in records:
            if not isinstance(record, dict):
                raise ValueError('each capture record must be an object')
            table = record.get('type')
            if not isinstance(table, str) or table not in SCHEMA:
                raise ValueError(f'unknown record type: {table}')
            fields = dict(field.split() for field in SCHEMA[table].split(', '))
            fields = {'id': 'TEXT', 'rev': 'INTEGER', **fields}
            data = {key: value for key, value in record.items() if key != 'type'}
            if data.keys() != fields.keys():
                raise ValueError(f'{table}: expected fields {", ".join(fields)}')
            for key, dtype in fields.items():
                value = data[key]
                if dtype == 'TEXT' and (not isinstance(value, str) or not value.strip()):
                    raise ValueError(f'{table}.{key}: nonempty text required')
                if dtype == 'INTEGER' and (type(value) is not int or not 0 <= value < 2**63):
                    raise ValueError(f'{table}.{key}: nonnegative SQLite integer required')
            validate(table, data)
            prior = db.execute(f'SELECT * FROM {table} WHERE id=? AND rev=?',
                               (data['id'], data['rev'])).fetchone()
            if prior is not None:
                if dict(prior) != data:
                    raise ValueError(f'conflicting {table} {data["id"]} revision {data["rev"]}')
                continue
            db.execute(f'INSERT INTO {table} VALUES ({",".join("?" for _ in fields)})',
                       tuple(data[key] for key in fields))
            added += 1
    return {'inserted': added, 'replayed': len(records) - added}


def validate(table, data):
    if data['rev'] < 1:
        raise ValueError('revision must start at one')
    for lo, hi in [('lo', 'hi'), ('x0', 'x1'), ('first', 'last')]:
        if lo in data and data[lo] >= data[hi]:
            raise ValueError(f'{table}: {lo} must be less than {hi}')
    if data.get('verified', 0) not in (0, 1):
        raise ValueError('verified must be 0 or 1')
    if any(data.get(key, 1) == 0 for key in ('width', 'grams', 'target', 'cents')):
        raise ValueError(f'{table}: physical quantity or claim must be positive')
    if table == 'hit' and data['grade'] not in ('reject', 'observe'):
        raise ValueError('unknown defect grade')
    if table == 'incident' and data['kind'] not in ('splice', 'break'):
        raise ValueError('unknown reel incident')
