import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .claims import Claims
from .finishing import Finishing, issue, weigh
from .inspection import Inspection
from .machine import Machine
from .store import connect, ingest, transaction


def build(db):
    machine = Machine(db)
    inspection = Inspection(db, machine)
    finishing = Finishing(db, machine, inspection)
    return machine, inspection, finishing, Claims(db, machine, inspection, finishing)


def recompute(db):
    machine, scanner, finishing, claims = build(db)
    return {'reels': [machine.assess(key) for key in machine.reels],
            'defects': [scanner.assess(key) for key in sorted({hit['defect'] for hit in scanner.hits})],
            'children': [finishing.assess(key) for key in finishing.lanes],
            'claims': [claims.assess(key) for key in claims.claims]}


def demo(db, fixture):
    data = json.loads(Path(fixture).read_text(encoding='utf-8-sig'))
    ingest(db, data['capture'])
    for lane, grams in data['weights'].items():
        with transaction(db, write=False):
            plan = build(db)[2].plan(lane)['plan']
        weigh(db, lane, 'demo-' + lane, 1, grams, plan)
    for lane in data['issue']:
        with transaction(db, write=False):
            basis = build(db)[2].assess(lane)['basis']
        issue(db, build, lane, basis, 'demo-operator')
    with transaction(db, write=False):
        return recompute(db)


def parser():
    cli = argparse.ArgumentParser(description='Offline paper-machine evidence')
    cli.add_argument('--db', required=True)
    sub = cli.add_subparsers(dest='command', required=True)
    sub.add_parser('demo').add_argument('fixture')
    sub.add_parser('ingest').add_argument('file')
    for command in ('reel', 'scan', 'finish', 'claim', 'plan'):
        sub.add_parser(command).add_argument('id')
    sub.add_parser('recompute')
    weight = sub.add_parser('weigh')
    for key in ('lane', 'ticket', 'plan'):
        weight.add_argument(key)
    weight.add_argument('rev', type=int)
    weight.add_argument('grams', type=int)
    sign = sub.add_parser('issue')
    for key in ('lane', 'basis', 'operator'):
        sign.add_argument(key)
    return cli


def run(db, args):
    cmd = args.command
    if cmd == 'demo':
        return demo(db, args.fixture)
    if cmd == 'ingest':
        return ingest(db, json.loads(Path(args.file).read_text(encoding='utf-8-sig')))
    if cmd == 'weigh':
        return weigh(db, args.lane, args.ticket, args.rev, args.grams, args.plan)
    if cmd == 'issue':
        return {'issue': issue(db, build, args.lane, args.basis, args.operator)}
    with transaction(db, write=False):
        if cmd == 'recompute':
            return recompute(db)
        machine, scanner, finishing, claims = build(db)
        action = {'reel': machine.assess, 'scan': scanner.assess, 'finish': finishing.assess,
                  'claim': claims.assess, 'plan': finishing.plan}[cmd]
        return action(args.id)


def main():
    cli = parser()
    args = cli.parse_args()
    try:
        with closing(connect(args.db, create=args.command in ('demo', 'ingest'))) as db:
            result = run(db, args)
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        cli.exit(2, f'error: {exc}\n')
    print(json.dumps(result, sort_keys=True, ensure_ascii=True))


if __name__ == '__main__':
    main()
