from collections import defaultdict
from hashlib import sha256

from app.errors import AuditError
from app.manifest import canonical, now
from app.modules.appeals.service import appeal_balances
from app.reasons import REASONS

OPEN = 'OPEN'
HELD = 'HELD'
CLOSED = 'CLOSED'
SETTLED = 'SETTLED'
REVERSED = 'REVERSED'
AMOUNT_FIELDS = ('original_cents', 'reduction_cents', 'net_cents', 'closed_cents', 'held_cents',
                 'previously_closed_cents', 'close_cents')
APPEAL_FIELDS = ('appeal_version', 'appeal_state', 'appeal_request_id', 'appeal_actor', 'appeal_note',
                 'appeal_evidence_ref', 'appeal_created_at')
DISPOSITION_LABELS = {CLOSED: '本次入账', HELD: '留待复核', SETTLED: '此前已结', REVERSED: '追回冲账'}


def latest_events(snapshot):
    """异议事件只追加；每柜最后一次事件即当前依据。"""
    events = {}
    for event in snapshot['events']:
        events[event['key']] = event
    return events


def ledger_key(snapshot):
    """异议台账指纹：每个抽中柜位的当前异议版本参与计算，任何新事件都会生成新的台账版本。"""
    versions = {balance['key']: [balance['version'], balance['status']]
                for balance in appeal_balances(snapshot)['rows']}
    key = {'run_id': snapshot['manifest']['run_id'], 'rule_version': snapshot['manifest']['rule_version'],
           'versions': dict(sorted(versions.items()))}
    return sha256(canonical(key).encode()).hexdigest()


def closing_id(run_id, ledger):
    return sha256(canonical({'run_id': run_id, 'ledger': ledger}).encode()).hexdigest()


def prior_bookings(published):
    """同一批次此前各版结转实际入账的金额，按柜位累计，用于只结增量而不重复计入。"""
    booked = defaultdict(int)
    for record in published:
        for row in record['rows']:
            booked[row['key']] += row['close_cents']
    return booked


def _totals(rows):
    totals = {field: 0 for field in AMOUNT_FIELDS}
    totals['row_count'] = len(rows)
    totals['closed_rows'] = sum(row['disposition'] in (CLOSED, SETTLED) for row in rows)
    totals['held_rows'] = sum(row['held_cents'] > 0 for row in rows)
    totals['reversed_rows'] = sum(row['disposition'] == REVERSED for row in rows)
    for row in rows:
        for field in AMOUNT_FIELDS:
            totals[field] += row[field]
    return totals


def _check(snapshot, totals):
    if totals['closed_cents'] + totals['held_cents'] != totals['net_cents']:
        raise AuditError('CLOSING_LEDGER_MISMATCH', '已可结与留待复核金额未覆盖结算净额', 500, totals)
    if totals['previously_closed_cents'] + totals['close_cents'] != totals['closed_cents']:
        raise AuditError('CLOSING_LEDGER_MISMATCH', '本次入账与此前已结金额不符', 500, totals)
    if totals['original_cents'] - totals['reduction_cents'] != totals['net_cents']:
        raise AuditError('CLOSING_LEDGER_MISMATCH', '原罚额与减免未勾稽到净额', 500, totals)
    if totals['original_cents'] != snapshot['settlement']['original_cents']:
        raise AuditError('CLOSING_LEDGER_MISMATCH', '结转原罚额与结算批次不符', 500, totals['original_cents'])


def _store_rows(rows, intake, campaign):
    titles = {row['store']: row['title'] for row in intake['stores']}
    groups = {group['code']: group['title'] for group in campaign['groups']}
    stores = {}
    for row in rows:
        entry = stores.setdefault(row['store'], {
            'store': row['store'], 'title': titles.get(row['store'], row['store']),
            'group': row['group'], 'group_title': groups.get(row['group'], row['group']),
            **{field: 0 for field in AMOUNT_FIELDS}, 'row_count': 0, 'closed_rows': [], 'held_rows': [],
        })
        for field in AMOUNT_FIELDS:
            entry[field] += row[field]
        entry['row_count'] += 1
        view = {'key': row['key'], 'rule': row['rule'], 'rule_label': row['rule_label'], 'plan': row['plan'],
                'plan_source': row['plan_source'], 'original_cents': row['original_cents'],
                'reduction_cents': row['reduction_cents'], 'net_cents': row['net_cents'],
                'held_cents': row['held_cents'], 'close_cents': row['close_cents'],
                **{field: row[field] for field in APPEAL_FIELDS}}
        if row['held_cents']:
            entry['held_rows'].append(view)
        if not row['held_cents'] or row['close_cents']:
            entry['closed_rows'].append(view)
    ordered = []
    for store in sorted(stores):
        entry = stores[store]
        entry['closed_shelf_count'] = len(entry['closed_rows'])
        entry['held_shelf_count'] = len(entry['held_rows'])
        if entry['held_cents']:
            entry['status'] = 'PARTIAL' if entry['close_cents'] > 0 else 'HELD'
        elif entry['close_cents'] > 0:
            entry['status'] = CLOSED
        elif entry['close_cents'] < 0:
            entry['status'] = REVERSED
        else:
            entry['status'] = SETTLED
        if entry['closed_cents'] + entry['held_cents'] != entry['net_cents']:
            raise AuditError('CLOSING_LEDGER_MISMATCH', '门店已可结与留待金额未覆盖净额', 500, entry['store'])
        ordered.append(entry)
    return ordered


def build_closing(snapshot, published, created_at=None, persisted=False):
    """按当前异议台账拆分门店已可结与留待复核金额；published 为同批次已发布的历史结转版本。"""
    created_at = created_at or now()
    balances = {row['key']: row for row in appeal_balances(snapshot)['rows']}
    penalties = {row['key']: row for row in snapshot['penalties']['rows'] if row['status'] == 'VIOLATION'}
    events = latest_events(snapshot)
    booked = prior_bookings(published)
    titles = {row['store']: row['title'] for row in snapshot['intake']['stores']}
    rows = []
    for key in sorted(balances):
        balance = balances[key]
        base = penalties[key]
        event = events.get(key)
        state = balance['status']
        original, reduction, net = balance['original_cents'], balance['reduction_cents'], balance['net_cents']
        # 只有仍在复核中的异议留待；已裁定或未发起异议的柜位都已可结。
        held = net if state == OPEN else 0
        settleable = 0 if state == OPEN else net
        previous = booked.get(key, 0)
        close = settleable - previous
        if state == OPEN:
            disposition = REVERSED if close < 0 else HELD
        else:
            disposition = CLOSED if close > 0 else (REVERSED if close < 0 else SETTLED)
        rows.append({
            'key': key, 'store': base['store'], 'store_title': titles.get(base['store'], base['store']),
            'slot': base['slot'], 'group': base['group'],
            'category': base['category'], 'rule': base['reason'], 'rule_label': REASONS.get(base['reason'], base['reason']),
            'plan': base['plan']['plan'] if base['plan'] else None,
            'plan_source': base['plan']['source']['id'] if base['plan'] else None,
            'original_cents': original, 'reduction_cents': reduction, 'net_cents': net,
            'settleable_cents': settleable, 'closed_cents': settleable, 'held_cents': held,
            'previously_closed_cents': previous, 'close_cents': close, 'disposition': disposition,
            'disposition_label': DISPOSITION_LABELS[disposition],
            'appeal_version': balance['version'], 'appeal_state': state,
            'appeal_request_id': event['request_id'] if event else None,
            'appeal_actor': event['actor'] if event else None,
            'appeal_note': event['note'] if event else None,
            'appeal_evidence_ref': event['evidence_ref'] if event else None,
            'appeal_created_at': event['created_at'] if event else None,
        })
    totals = _totals(rows)
    _check(snapshot, totals)
    stores = _store_rows(rows, snapshot['intake'], snapshot['campaign'])
    ledger = ledger_key(snapshot)
    campaign = snapshot['campaign']
    return {
        'format': 'shelf-closing/1', 'closing_id': closing_id(snapshot['manifest']['run_id'], ledger),
        'run_id': snapshot['manifest']['run_id'], 'revision': len(published) + 1, 'persisted': persisted,
        'created_at': created_at, 'balanced': True,
        'period': {'code': campaign['code'], 'version': campaign['version'], 'title': campaign['title'],
                   'start_utc': campaign['start_utc'], 'end_utc': campaign['end_utc']},
        'basis': {'rule_version': snapshot['manifest']['rule_version'], 'ledger': ledger, 'cutoff_at': created_at,
                  'versions': {key: balance['version'] for key, balance in sorted(balances.items())},
                  'states': {key: balance['status'] for key, balance in sorted(balances.items())},
                  'prior_closings': [record['closing_id'] for record in published]},
        'manifest': snapshot['manifest'], 'totals': totals, 'stores': stores, 'rows': rows,
        'held': [row for row in rows if row['held_cents'] > 0],
    }


def plan_closing(snapshot, current, published):
    """发布入口：台账版本未变则原样取回上一版，绝不重复计入已结金额。"""
    ledger = ledger_key(snapshot)
    if current is not None and current['basis']['ledger'] == ledger:
        return {**current, 'reused': True}, True
    record = build_closing(snapshot, published, persisted=True)
    return {**record, 'reused': False}, False


def preview_closing(snapshot, published):
    ledger = ledger_key(snapshot)
    if published and published[-1]['basis']['ledger'] == ledger:
        return {**published[-1], 'reused': True}
    return {**build_closing(snapshot, published), 'reused': False}


def statement(record):
    """财务结转单：含已结、留待、原罚额与异议依据，可脱离批次快照回查。"""
    return {
        'format': 'shelf-closing-statement/1', 'closing_id': record['closing_id'], 'run_id': record['run_id'],
        'revision': record['revision'], 'created_at': record['created_at'], 'period': record['period'],
        'basis': record['basis'], 'totals': record['totals'], 'rows': record['rows'],
        'stores': record['stores'], 'held': record['held'], 'reason_dictionary': REASONS,
    }


def summary(record):
    return {'closing_id': record['closing_id'], 'run_id': record['run_id'], 'revision': record['revision'],
            'created_at': record['created_at'], 'period': record['period'],
            'ledger': record['basis']['ledger'], 'totals': record['totals']}
