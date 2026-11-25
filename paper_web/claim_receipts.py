"""After-sales credit/refund receipt posting against claim balances.

Receipts are offline JSON documents carrying integer cents.  A credit receipt
names the set of claims it is authorized to deduct; the after-sales station
selects the per-claim allocation when posting.  A refund receipt names the
original credit and the claim whose share it returns, so refunds always draw
on that specific credit's landed share, never on another credit.

Signed entries are append-only: a receipt revision never rewrites an entry
made under an older revision.  When shop-floor evidence changes the confirmed
sub-roll amount reported by :mod:`paper_web.claims`, the old ledger stays
queryable and the difference is reported instead of being reallocated away.
"""

import json
from collections import Counter

from .store import canonical, digest, transaction


CREDIT = 'credit'
REFUND = 'refund'

_DDL = (
    """CREATE TABLE IF NOT EXISTS credit_doc (
        id TEXT NOT NULL, rev INTEGER NOT NULL, cents INTEGER NOT NULL,
        doc TEXT NOT NULL, PRIMARY KEY (id, rev))""",
    """CREATE TABLE IF NOT EXISTS credit_auth (
        credit TEXT NOT NULL, rev INTEGER NOT NULL, claim TEXT NOT NULL,
        PRIMARY KEY (credit, rev, claim))""",
    """CREATE TABLE IF NOT EXISTS refund_doc (
        id TEXT NOT NULL, rev INTEGER NOT NULL, cents INTEGER NOT NULL,
        credit TEXT NOT NULL, credit_rev INTEGER NOT NULL, claim TEXT NOT NULL,
        doc TEXT NOT NULL, PRIMARY KEY (id, rev))""",
    """CREATE TABLE IF NOT EXISTS receipt_entry (
        seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, receipt TEXT NOT NULL,
        rev INTEGER NOT NULL, claim TEXT, credit TEXT, credit_rev INTEGER,
        cents INTEGER NOT NULL, witness TEXT NOT NULL, operator TEXT NOT NULL,
        detail TEXT NOT NULL, UNIQUE (receipt, rev))""",
    """CREATE TABLE IF NOT EXISTS receipt_line (
        seq INTEGER NOT NULL, claim TEXT NOT NULL,
        credit_cents INTEGER NOT NULL, refund_cents INTEGER NOT NULL,
        PRIMARY KEY (seq, claim))""",
)


def ensure(db):
    for statement in _DDL:
        db.execute(statement)


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label}: nonempty text required')
    return value


def _positive_cents(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f'{label}: positive integer cents required')
    return value


def _revision(value):
    if type(value) is not int or value < 1:
        raise ValueError('revision must start at one')
    return value


def _receipt_id_used(db, receipt_id):
    for table in ('credit_doc', 'refund_doc'):
        if db.execute(f'SELECT 1 FROM {table} WHERE id=? LIMIT 1', (receipt_id,)).fetchone():
            return table
    return None


def receive(db, records):
    """Save receipt originals. Same id/rev replays; conflicting content rolls back."""
    if not isinstance(records, list):
        raise ValueError('receipt batch must contain an array')
    ensure(db)
    inserted = 0
    with transaction(db):
        for record in records:
            if not isinstance(record, dict):
                raise ValueError('each receipt must be an object')
            kind = record.get('type')
            if kind == CREDIT:
                if set(record) != {'type', 'id', 'rev', 'cents', 'claims'}:
                    raise ValueError('credit: expected fields type,id,rev,cents,claims')
                receipt_id = _text(record['id'], 'credit.id')
                _revision(record['rev'])
                _positive_cents(record['cents'], 'credit.cents')
                claims = record['claims']
                if (not isinstance(claims, list) or not claims
                        or any(not isinstance(c, str) or not c.strip() for c in claims)
                        or len(set(claims)) != len(claims)):
                    raise ValueError('credit.claims: nonempty distinct claim ids required')
                owner = _receipt_id_used(db, receipt_id)
                if owner == 'refund_doc':
                    raise ValueError(f'receipt id already used by a refund: {receipt_id}')
                existing = db.execute(
                    'SELECT doc FROM credit_doc WHERE id=? AND rev=?',
                    (receipt_id, record['rev'])).fetchone()
                if existing is not None:
                    if json.loads(existing['doc']) != record:
                        raise ValueError(
                            f'conflicting credit {receipt_id} revision {record["rev"]}')
                    continue
                db.execute('INSERT INTO credit_doc(id,rev,cents,doc) VALUES (?,?,?,?)',
                           (receipt_id, record['rev'], record['cents'], canonical(record)))
                for claim in claims:
                    db.execute('INSERT INTO credit_auth(credit,rev,claim) VALUES (?,?,?)',
                               (receipt_id, record['rev'], claim))
                inserted += 1
            elif kind == REFUND:
                if set(record) != {'type', 'id', 'rev', 'cents', 'credit',
                                   'credit_rev', 'claim'}:
                    raise ValueError(
                        'refund: expected fields type,id,rev,cents,credit,credit_rev,claim')
                receipt_id = _text(record['id'], 'refund.id')
                _revision(record['rev'])
                _positive_cents(record['cents'], 'refund.cents')
                _text(record['credit'], 'refund.credit')
                _revision(record['credit_rev'])
                _text(record['claim'], 'refund.claim')
                owner = _receipt_id_used(db, receipt_id)
                if owner == 'credit_doc':
                    raise ValueError(f'receipt id already used by a credit: {receipt_id}')
                existing = db.execute(
                    'SELECT doc FROM refund_doc WHERE id=? AND rev=?',
                    (receipt_id, record['rev'])).fetchone()
                if existing is not None:
                    if json.loads(existing['doc']) != record:
                        raise ValueError(
                            f'conflicting refund {receipt_id} revision {record["rev"]}')
                    continue
                db.execute(
                    'INSERT INTO refund_doc(id,rev,cents,credit,credit_rev,claim,doc) '
                    'VALUES (?,?,?,?,?,?,?)',
                    (receipt_id, record['rev'], record['cents'], record['credit'],
                     record['credit_rev'], record['claim'], canonical(record)))
                inserted += 1
            else:
                raise ValueError(f'unknown receipt type: {kind}')
    return {'inserted': inserted, 'replayed': len(records) - inserted}


class Receipts:
    def __init__(self, db, claims):
        ensure(db)
        self.db = db
        self.claims = claims
        self._reload()

    def _reload(self):
        self.credit_docs = self._load_credits()
        self.refund_docs = self._load_refunds()
        self.entries = [dict(row) for row in
                        self.db.execute('SELECT * FROM receipt_entry ORDER BY seq')]
        self.lines = {
            entry['seq']: [dict(row) for row in self.db.execute(
                'SELECT * FROM receipt_line WHERE seq=? ORDER BY claim', (entry['seq'],))]
            for entry in self.entries}

    def _load_credits(self):
        credits = {}
        rows = self.db.execute(
            'SELECT a.* FROM credit_doc a JOIN (SELECT id, max(rev) AS rev '
            'FROM credit_doc GROUP BY id) b USING(id, rev) ORDER BY a.id')
        for row in rows:
            auth = [r['claim'] for r in self.db.execute(
                'SELECT claim FROM credit_auth WHERE credit=? AND rev=? ORDER BY claim',
                (row['id'], row['rev']))]
            credits[row['id']] = {'id': row['id'], 'rev': row['rev'],
                                  'cents': row['cents'], 'auth': auth,
                                  'doc': json.loads(row['doc'])}
        return credits

    def _load_refunds(self):
        return {row['id']: {'id': row['id'], 'rev': row['rev'], 'cents': row['cents'],
                            'credit': row['credit'], 'credit_rev': row['credit_rev'],
                            'claim': row['claim'], 'doc': json.loads(row['doc'])}
                for row in self.db.execute(
                    'SELECT a.* FROM refund_doc a JOIN (SELECT id, max(rev) AS rev '
                    'FROM refund_doc GROUP BY id) b USING(id, rev) ORDER BY a.id')}

    # ------------------------------------------------------------------ ledger
    def entry_for(self, receipt, rev):
        return next((entry for entry in self.entries
                     if entry['receipt'] == receipt and entry['rev'] == rev), None)

    def entries_for(self, receipt):
        return [entry for entry in self.entries if entry['receipt'] == receipt]

    def _credit_lines(self, credit=None):
        for entry in self.entries:
            if entry['kind'] != CREDIT:
                continue
            if credit is not None and entry['receipt'] != credit:
                continue
            for line in self.lines[entry['seq']]:
                yield entry, line

    def signed_gross(self, claim):
        return sum(line['credit_cents'] for _, line in self._credit_lines()
                   if line['claim'] == claim)

    def credit_share(self, credit, claim):
        """Cents actually landed on claim under the named credit, all signed revs."""
        return sum(line['credit_cents'] for entry, line in self._credit_lines(credit)
                   if line['claim'] == claim)

    def booked_refunds(self, credit, claim):
        return sum(entry['cents'] for entry in self.entries
                   if entry['kind'] == REFUND and entry['credit'] == credit
                   and entry['claim'] == claim)

    def bound(self, claim_id):
        """Current confirmed sub-roll cents; held/reserve shares never count."""
        if claim_id not in self.claims.claims:
            return None
        allocation = self.claims.assess(claim_id)['allocation']
        return sum(cents for key, cents in allocation.items() if key.startswith('child:'))

    def pending_refunds(self, credit, rev=None):
        return [doc for doc in self.refund_docs.values() if doc['credit'] == credit
                and (rev is None or doc['credit_rev'] == rev)
                and self.entry_for(doc['id'], doc['rev']) is None]

    def _status(self, receipt_id, rev):
        if self.entry_for(receipt_id, rev) is not None:
            return 'signed'
        if self.entries_for(receipt_id):
            return 'stale'
        return 'pending'

    # ------------------------------------------------------------------- views
    def assess(self, receipt_id):
        if receipt_id in self.credit_docs:
            return self._credit_view(receipt_id)
        if receipt_id in self.refund_docs:
            return self._refund_view(receipt_id)
        raise KeyError(receipt_id)

    def _signed_summaries(self, receipt_id):
        summaries = []
        for entry in self.entries_for(receipt_id):
            detail = json.loads(entry['detail'])
            summaries.append({'seq': entry['seq'], 'rev': entry['rev'],
                              'witness': entry['witness'],
                              'allocation': detail.get('allocation')})
        return summaries

    def _credit_view(self, receipt_id):
        doc = self.credit_docs[receipt_id]
        reasons = []
        bounds = {}
        available = {}
        for claim in doc['auth']:
            bound = self.bound(claim)
            bounds[claim] = bound
            if bound is None:
                reasons.append('unknown-claim:' + claim)
            else:
                available[claim] = max(0, bound - self.signed_gross(claim))
        reserve = Counter()
        pending = []
        for refund in self.pending_refunds(receipt_id, doc['rev']):
            pending.append({'receipt': refund['id'], 'rev': refund['rev'],
                            'claim': refund['claim'], 'cents': refund['cents']})
            if self.bound(refund['claim']) is None:
                reasons.append('refund-unknown-claim:' + refund['id'])
            if refund['claim'] not in doc['auth']:
                reasons.append('refund-outside-authorization:' + refund['id'])
            reserve[refund['claim']] += refund['cents']
        signed_claims = {line['claim'] for _, line in self._credit_lines(receipt_id)}
        for claim in sorted(signed_claims - set(doc['auth'])):
            reasons.append('authorization-narrowed:' + claim)
        return {'receipt': receipt_id, 'kind': CREDIT, 'rev': doc['rev'],
                'status': self._status(receipt_id, doc['rev']),
                'cents': doc['cents'], 'authorized': doc['auth'],
                'bounds': bounds, 'available': dict(sorted(available.items())),
                'reserve': dict(sorted(reserve.items())),
                'pending_refunds': pending,
                'entries': self._signed_summaries(receipt_id), 'reasons': reasons}

    def _refund_view(self, receipt_id):
        doc = self.refund_docs[receipt_id]
        reasons = []
        auth = self._auth_at(doc['credit'], doc['credit_rev'])
        if auth is None:
            reasons.append(f"unknown-credit:{doc['credit']}@{doc['credit_rev']}")
        elif doc['claim'] not in auth:
            reasons.append(
                f"outside-authorization:{doc['credit']}@{doc['credit_rev']}")
        share = self.credit_share(doc['credit'], doc['claim'])
        booked = self.booked_refunds(doc['credit'], doc['claim'])
        if auth is not None and share == 0:
            reasons.append('credit-not-posted:' + doc['credit'])
        if share - booked < doc['cents'] and self.entry_for(doc['id'], doc['rev']) is None:
            reasons.append(f'credit-share-exceeded:{doc["credit"]}/{doc["claim"]}')
        return {'receipt': receipt_id, 'kind': REFUND, 'rev': doc['rev'],
                'status': self._status(receipt_id, doc['rev']),
                'cents': doc['cents'], 'credit': doc['credit'],
                'credit_rev': doc['credit_rev'], 'claim': doc['claim'],
                'share_cents': share, 'booked_refund_cents': booked,
                'available_cents': max(0, share - booked),
                'entries': self._signed_summaries(receipt_id), 'reasons': reasons}

    def _auth_at(self, credit, rev):
        row = self.db.execute(
            'SELECT 1 FROM credit_doc WHERE id=? AND rev=?', (credit, rev)).fetchone()
        if row is None:
            return None
        return [r['claim'] for r in self.db.execute(
            'SELECT claim FROM credit_auth WHERE credit=? AND rev=? ORDER BY claim',
            (credit, rev))]

    def check(self, receipt_id, allocation):
        """Validate a tentative allocation; returns normalized lines and reasons."""
        doc = self.credit_docs[receipt_id]
        if not isinstance(allocation, dict):
            raise ValueError('allocation must be an object')
        reasons = []
        normalized = {claim: 0 for claim in doc['auth']}
        for claim, cents in allocation.items():
            if type(cents) is not int or cents < 0:
                raise ValueError(f'allocation.{claim}: nonnegative integer cents required')
            if claim not in doc['auth']:
                reasons.append('outside-authorization:' + claim)
                continue
            normalized[claim] = cents
        missing = sorted(set(doc['auth']) - set(allocation))
        for claim in missing:
            reasons.append('missing-allocation:' + claim)
        if sum(normalized.values()) != doc['cents']:
            reasons.append('allocation-total')
        for claim in doc['auth']:
            bound = self.bound(claim)
            if bound is None:
                reasons.append('unknown-claim:' + claim)
            elif self.signed_gross(claim) + normalized[claim] > bound:
                reasons.append('bound-exceeded:' + claim)
        reserve = Counter()
        for refund in self.pending_refunds(receipt_id, doc['rev']):
            if self.bound(refund['claim']) is None:
                reasons.append('refund-unknown-claim:' + refund['id'])
            if refund['claim'] not in doc['auth']:
                reasons.append('refund-outside-authorization:' + refund['id'])
            reserve[refund['claim']] += refund['cents']
        for claim, need in reserve.items():
            if normalized.get(claim, 0) < need + self.booked_refunds(receipt_id, claim):
                reasons.append('refund-reserve:' + claim)
        return dict(sorted(normalized.items())), reasons

    # --------------------------------------------------------------- settlement
    def _insert(self, kind, receipt, rev, cents, detail, operator,
                claim=None, credit=None, credit_rev=None):
        witness = digest(detail)
        seq = self.db.execute(
            'INSERT INTO receipt_entry(kind,receipt,rev,claim,credit,credit_rev,cents,'
            'witness,operator,detail) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (kind, receipt, rev, claim, credit, credit_rev, cents, witness, operator,
             json.dumps(detail, sort_keys=True, ensure_ascii=False))).lastrowid
        return seq, witness

    def settle_credit(self, receipt_id, allocation, operator):
        doc = self.credit_docs[receipt_id]
        normalized, reasons = self.check(receipt_id, allocation)
        existing = self.entry_for(receipt_id, doc['rev'])
        if existing is not None:
            old = json.loads(existing['detail'])
            if old['allocation'] == normalized:
                return {'replayed': True, 'entries': [self._entry_result(existing)]}
            raise ValueError(f'credit {receipt_id} rev {doc["rev"]} is locked; '
                             'signed shares cannot be redistributed')
        if reasons:
            raise ValueError('receipt held: ' + ', '.join(reasons))
        pending = sorted(self.pending_refunds(receipt_id, doc['rev']),
                         key=lambda refund: refund['id'])
        refund_totals = Counter()
        for refund in pending:
            refund_totals[refund['claim']] += refund['cents']
        lines = []
        for claim in doc['auth']:
            refunded = (self.booked_refunds(receipt_id, claim) + refund_totals[claim])
            lines.append({'claim': claim, 'credit_cents': normalized[claim],
                          'refund_cents': refunded,
                          'net_cents': normalized[claim] - refunded})
        detail = {'kind': CREDIT, 'receipt': receipt_id, 'rev': doc['rev'],
                  'cents': doc['cents'], 'authorized': doc['auth'],
                  'allocation': normalized,
                  'bounds': {claim: self.bound(claim) for claim in doc['auth']},
                  'gross_before': {claim: self.signed_gross(claim) for claim in doc['auth']},
                  'refunds': [{'receipt': r['id'], 'rev': r['rev'], 'claim': r['claim'],
                               'cents': r['cents']} for r in pending],
                  'lines': lines, 'operator': operator}
        seq, witness = self._insert(CREDIT, receipt_id, doc['rev'], doc['cents'],
                                    detail, operator)
        for line in lines:
            self.db.execute(
                'INSERT INTO receipt_line(seq,claim,credit_cents,refund_cents) '
                'VALUES (?,?,?,?)',
                (seq, line['claim'], line['credit_cents'], line['refund_cents']))
        entry = dict(self.db.execute('SELECT * FROM receipt_entry WHERE seq=?',
                                     (seq,)).fetchone())
        results = [self._entry_result(entry)]
        self._reload()
        for refund in pending:
            results.append(self._insert_refund(refund, operator))
        self._reload()
        net = {line['claim']: line['net_cents'] for line in lines}
        return {'replayed': False, 'witness': witness, 'net': dict(sorted(net.items())),
                'entries': results}

    def _insert_refund(self, refund, operator):
        auth = self._auth_at(refund['credit'], refund['credit_rev'])
        if auth is None:
            raise ValueError(
                f"refund {refund['id']}: unknown credit "
                f"{refund['credit']}@{refund['credit_rev']}")
        if refund['claim'] not in auth:
            raise ValueError(
                f"refund {refund['id']}: credit {refund['credit']} rev "
                f"{refund['credit_rev']} never authorized {refund['claim']}")
        share = self.credit_share(refund['credit'], refund['claim'])
        booked = self.booked_refunds(refund['credit'], refund['claim'])
        if share == 0:
            raise ValueError(
                f"refund {refund['id']}: credit {refund['credit']} has no signed "
                f"share on {refund['claim']}; post the credit first")
        if share - booked < refund['cents']:
            raise ValueError(
                f'refund {refund["id"]}: credit {refund["credit"]} landed {share} cents on '
                f'{refund["claim"]}, {booked} already returned, {refund["cents"]} requested')
        detail = {'kind': REFUND, 'receipt': refund['id'], 'rev': refund['rev'],
                  'cents': refund['cents'], 'credit': refund['credit'],
                  'credit_rev': refund['credit_rev'], 'claim': refund['claim'],
                  'share_cents': share, 'booked_cents': booked,
                  'cumulative_cents': booked + refund['cents'], 'operator': operator}
        seq, witness = self._insert(REFUND, refund['id'], refund['rev'], refund['cents'],
                                    detail, operator, claim=refund['claim'],
                                    credit=refund['credit'],
                                    credit_rev=refund['credit_rev'])
        self.db.execute(
            'INSERT INTO receipt_line(seq,claim,credit_cents,refund_cents) VALUES (?,?,?,?)',
            (seq, refund['claim'], 0, refund['cents']))
        return {'seq': seq, 'kind': REFUND, 'receipt': refund['id'], 'rev': refund['rev'],
                'witness': witness}

    def _entry_result(self, entry):
        return {'seq': entry['seq'], 'kind': entry['kind'], 'receipt': entry['receipt'],
                'rev': entry['rev'], 'witness': entry['witness']}

    def settle_one(self, receipt_id, allocation, operator):
        if receipt_id in self.credit_docs:
            return self.settle_credit(receipt_id, allocation, operator)
        if receipt_id in self.refund_docs:
            if allocation:
                raise ValueError('refund receipts take no allocation')
            doc = self.refund_docs[receipt_id]
            existing = self.entry_for(doc['id'], doc['rev'])
            if existing is not None:
                return {'replayed': True, 'entries': [self._entry_result(existing)]}
            result = self._insert_refund(doc, operator)
            return {'replayed': False, 'witness': result['witness'], 'entries': [result]}
        raise KeyError(receipt_id)

    # ---------------------------------------------------------------- overview
    def overview(self):
        receipts = [self.assess(receipt_id) for receipt_id in
                    sorted(set(self.credit_docs) | set(self.refund_docs))]
        claim_ids = sorted({line['claim'] for entry in self.entries
                            for line in self.lines[entry['seq']]})
        claim_ledger = {}
        discrepancies = []
        for claim in claim_ids:
            gross = self.signed_gross(claim)
            refunds = sum(entry['cents'] for entry in self.entries
                          if entry['kind'] == REFUND and entry['claim'] == claim)
            bound = self.bound(claim)
            excess = None if bound is None else max(0, gross - bound)
            claim_ledger[claim] = {
                'bound_cents': bound, 'credit_cents': gross,
                'refund_cents': refunds, 'net_cents': gross - refunds,
                'available_cents': None if bound is None else max(0, bound - gross),
                'excess_cents': excess}
            if excess:
                discrepancies.append({'claim': claim, 'signed_cents': gross,
                                      'bound_cents': bound, 'excess_cents': excess})
        credit_ledger = {}
        for credit in sorted(self.credit_docs):
            per_claim = Counter()
            for _, line in self._credit_lines(credit):
                per_claim[line['claim']] += line['credit_cents']
            for entry in self.entries:
                if entry['kind'] == REFUND and entry['credit'] == credit:
                    per_claim[entry['claim']] -= entry['cents']
            credit_ledger[credit] = {'net_cents': dict(sorted(per_claim.items()))}
        return {'receipts': receipts,
                'ledger': {'claims': claim_ledger, 'credits': credit_ledger},
                'discrepancies': discrepancies}


def settle(db, build, receipt_id, allocation, operator):
    """Post one receipt with the selected details, locking a signed witness."""
    if not isinstance(operator, str) or not operator.strip():
        raise ValueError('operator required')
    ensure(db)
    with transaction(db):
        receipts = Receipts(db, build(db)[3])
        return receipts.settle_one(receipt_id, allocation, operator)
