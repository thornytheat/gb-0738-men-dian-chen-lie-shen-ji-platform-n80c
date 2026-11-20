import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.config import Options
from app.errors import AuditError
from app.manifest import canonical, manifest
from app.modules.appeals.transitions import next_event
from app.modules.settlement.closing import plan_closing
from app.pipeline import Audit
from app.store import Store
from conftest import appeal_payload


def path(run, suffix=''):
    return f"/api/settlement/runs/{run['manifest']['run_id']}/closings{suffix}"


def open_appeal(client, run):
    return client.post(f"/api/appeals/{run['manifest']['run_id']}/events", json=appeal_payload()).json()['event']


def reduce_appeal(client, run):
    return client.post(f"/api/appeals/{run['manifest']['run_id']}/events", json=appeal_payload(
        request_id='A-REDUCE-202611', expected_version=1, action='REDUCED', reduction_cents=6000,
        note='部分补证确实', evidence_ref='回查票-27')).json()['event']


def test_closing_splits_each_store_between_settled_and_held(client, run):
    opened = open_appeal(client, run)
    preview = client.get(path(run, '/preview')).json()
    assert preview['totals'] == {'original_cents': 29500, 'reduction_cents': 0, 'net_cents': 29500,
                                 'closed_cents': 14500, 'held_cents': 15000, 'previously_closed_cents': 0,
                                 'close_cents': 14500, 'row_count': 3, 'closed_rows': 2, 'held_rows': 1,
                                 'reversed_rows': 0}
    stores = {row['store']: row for row in preview['stores']}
    assert (stores['001']['close_cents'], stores['001']['held_cents'], stores['001']['status']) == (5500, 0, 'CLOSED')
    assert (stores['002']['close_cents'], stores['002']['held_cents'], stores['002']['status']) == (0, 15000, 'HELD')
    assert (stores['005']['close_cents'], stores['005']['held_cents'], stores['005']['status']) == (9000, 0, 'CLOSED')
    assert sum(row['net_cents'] for row in preview['stores']) == preview['totals']['net_cents']
    held, = preview['held']
    assert (held['key'], held['held_cents'], held['appeal_version'], held['appeal_state']) == ('002:A03', 15000, 1, 'OPEN')
    assert held['appeal_request_id'] == opened['request_id'] and held['appeal_evidence_ref'] == '复核簿-21'
    assert (held['original_cents'], held['plan_source']) == (15000, 'plans.csv:3')


def test_repeated_publish_of_same_ledger_never_double_counts(client, run):
    open_appeal(client, run)
    first = client.post(path(run)).json()
    assert (first['revision'], first['reused'], first['totals']['close_cents']) == (1, False, 14500)
    second = client.post(path(run)).json()
    assert (second['revision'], second['reused'], second['closing_id']) == (1, True, first['closing_id'])
    assert second['totals']['close_cents'] == 14500
    listing = client.get(path(run)).json()
    assert [(item['revision'], item['totals']['close_cents']) for item in listing['versions']] == [(1, 14500)]


def test_late_reduction_books_only_the_increment_with_prior_version_basis(client, run):
    open_appeal(client, run)
    first = client.post(path(run)).json()
    assert first['totals']['held_cents'] == 15000
    decision = reduce_appeal(client, run)
    preview = client.get(path(run, '/preview')).json()
    assert (preview['revision'], preview['reused']) == (2, False)
    assert (preview['totals']['close_cents'], preview['totals']['previously_closed_cents']) == (9000, 14500)
    assert preview['totals']['held_cents'] == 0 and preview['held'] == []
    row = next(item for item in preview['rows'] if item['key'] == '002:A03')
    assert (row['disposition'], row['close_cents'], row['appeal_version']) == ('CLOSED', 9000, 2)
    assert row['appeal_request_id'] == decision['request_id']
    assert preview['basis']['prior_closings'] == [first['closing_id']]
    assert preview['basis']['versions']['002:A03'] == 2
    second = client.post(path(run)).json()
    assert (second['revision'], second['reused']) == (2, False)
    assert second['totals']['closed_cents'] == 23500 and second['totals']['close_cents'] == 9000
    listing = client.get(path(run)).json()
    assert [item['totals']['close_cents'] for item in listing['versions']] == [14500, 9000]
    assert sum(item['totals']['close_cents'] for item in listing['versions']) == 23500


def test_late_appeal_after_store_was_closed_is_reversed_then_rebooked(client, run):
    first = client.post(path(run)).json()
    assert (first['totals']['close_cents'], first['totals']['held_cents']) == (29500, 0)
    open_appeal(client, run)
    reversal = client.post(path(run)).json()
    assert (reversal['revision'], reversal['totals']['close_cents']) == (2, -15000)
    assert (reversal['totals']['closed_cents'], reversal['totals']['held_cents']) == (14500, 15000)
    held = next(row for row in reversal['rows'] if row['key'] == '002:A03')
    assert (held['disposition'], held['close_cents'], held['previously_closed_cents']) == ('REVERSED', -15000, 15000)
    reduce_appeal(client, run)
    booked = client.post(path(run)).json()
    assert (booked['revision'], booked['totals']['close_cents']) == (3, 9000)
    assert (booked['totals']['previously_closed_cents'], booked['totals']['closed_cents']) == (14500, 23500)
    assert (booked['totals']['net_cents'], booked['totals']['held_cents']) == (23500, 0)
    listing = client.get(path(run)).json()
    assert sum(item['totals']['close_cents'] for item in listing['versions']) == 23500


def test_closing_without_any_penalty_is_balanced(client):
    reference = client.post('/api/settlement/runs', json={'dataset': 'reference'}).json()
    assert reference['settlement']['original_cents'] == 0
    preview = client.get(path(reference, '/preview')).json()
    assert preview['totals']['row_count'] == 0 and preview['stores'] == []
    published = client.post(path(reference)).json()
    assert (published['revision'], published['balanced']) == (1, True)
    assert published['totals']['close_cents'] == published['totals']['held_cents'] == 0


def test_reduction_after_store_was_already_closed_books_a_recovery(client, run):
    first = client.post(path(run)).json()
    assert (first['totals']['close_cents'], first['totals']['held_cents']) == (29500, 0)
    open_appeal(client, run)
    reduce_appeal(client, run)
    corrected = client.post(path(run)).json()
    assert (corrected['revision'], corrected['totals']['close_cents']) == (2, -6000)
    assert (corrected['totals']['closed_cents'], corrected['totals']['previously_closed_cents']) == (23500, 29500)
    row = next(item for item in corrected['rows'] if item['key'] == '002:A03')
    assert (row['disposition'], row['disposition_label'], row['close_cents']) == ('REVERSED', '追回冲账', -6000)
    assert (row['net_cents'], row['reduction_cents'], row['appeal_version']) == (9000, 6000, 2)
    replay = client.post(path(run)).json()
    assert (replay['revision'], replay['reused']) == (2, True)
    listing = client.get(path(run)).json()
    assert sum(item['totals']['close_cents'] for item in listing['versions']) == 23500


def test_closings_are_scoped_to_one_settlement_run(client, run):
    first = client.post(path(run)).json()
    other = client.post('/api/settlement/runs', json={'sample_size': 17}).json()
    assert other['manifest']['run_id'] != run['manifest']['run_id']
    assert client.get(path(other)).json()['versions'] == []
    rerun = client.post('/api/settlement/runs', json={}).json()
    assert rerun['reused'] is True
    listing = client.get(path(run)).json()
    assert [item['closing_id'] for item in listing['versions']] == [first['closing_id']]
    withheld = client.get(f"/api/settlement/runs/{other['manifest']['run_id']}/closings/{first['closing_id']}")
    assert (withheld.status_code, withheld.json()['detail']['code']) == (404, 'CLOSING_NOT_FOUND')


def test_statement_after_appeal_marks_the_held_version(client, run):
    open_appeal(client, run)
    published = client.post(path(run)).json()
    later = client.get(path(run, f"/{published['closing_id']}/statement")).json()
    assert later['basis']['versions']['002:A03'] == 1
    assert later['basis']['states']['002:A03'] == 'OPEN'
    assert later['held'][0]['appeal_request_id'] == 'A-OPEN-202611'
    assert later['totals']['held_cents'] == 15000


def test_held_version_is_named_on_the_finance_statement(client, run):
    open_appeal(client, run)
    published = client.post(path(run)).json()
    response = client.get(path(run, f"/{published['closing_id']}/statement?format=csv"))
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/csv')
    body = response.content.decode('utf-8-sig')
    assert '留待复核' in body and '异议版本' in body
    assert '002:A03' in body and 'A-OPEN-202611' in body and '复核簿-21' in body
    assert ',15000,0,15000,0,15000,OPEN,1,' in body
    exported = client.get(path(run, f"/{published['closing_id']}/statement")).json()
    assert exported['closing_id'] == published['closing_id']
    assert exported['basis']['versions']['002:A03'] == 1
    assert exported['period']['version'] == 'WINE-2611-A3'
    held, = exported['held']
    assert (held['original_cents'], held['rule'], held['plan_source']) == (15000, 'MISSING_SKU', 'plans.csv:3')


def test_original_penalty_and_appeal_evidence_remain_queryable_after_later_decision(client, run):
    open_appeal(client, run)
    published = client.post(path(run)).json()
    decision = reduce_appeal(client, run)
    first = client.get(path(run, f"/{published['closing_id']}")).json()
    assert first['totals']['held_cents'] == 15000
    row = next(item for item in first['rows'] if item['key'] == '002:A03')
    assert (row['original_cents'], row['appeal_state'], row['appeal_version']) == (15000, 'OPEN', 1)
    later = client.get(path(run, f"/{published['closing_id']}/statement")).json()
    assert later['basis']['versions']['002:A03'] == 1
    assert later['held'][0]['appeal_evidence_ref'] == '复核簿-21'


def test_failed_publication_leaves_previous_closing_intact(client, run, monkeypatch):
    open_appeal(client, run)
    published = client.post(path(run)).json()
    reduce_appeal(client, run)
    store = client.app.state.store

    calls = {'count': 0}
    original = Store.latest_closing

    def failing_verification(self, connection, run_id):
        calls['count'] += 1
        if calls['count'] == 2:
            raise AuditError('CLOSING_WRITE_FAILED', '模拟落盘校验失败', 500)
        return original(self, connection, run_id)

    monkeypatch.setattr(Store, 'latest_closing', failing_verification)
    response = client.post(path(run))
    assert (response.status_code, response.json()['detail']['code']) == (500, 'CLOSING_WRITE_FAILED')
    monkeypatch.undo()
    listing = client.get(path(run)).json()
    assert [(item['revision'], item['closing_id']) for item in listing['versions']] == [(1, published['closing_id'])]
    restored = client.get(path(run, f"/{published['closing_id']}")).json()
    assert (restored['totals']['close_cents'], restored['totals']['held_cents']) == (14500, 15000)
    retried = client.post(path(run)).json()
    assert (retried['revision'], retried['reused'], retried['totals']['close_cents']) == (2, False, 9000)
    assert store.read_closings(run['manifest']['run_id'])['latest']['revision'] == 2


def prepared_store(settings):
    store = Store(settings.database)
    store.initialize()
    audit = Audit(settings, Options())
    run = manifest(audit)
    store.save({'manifest': run, 'campaign': audit.intake['campaign'], 'intake': audit.intake, 'plans': audit.plans,
                'photos': audit.photos, 'coverage': audit.coverage, 'penalties': audit.penalties,
                'settlement': audit.settlement})
    return store, run


def test_corrupted_stored_closing_cannot_be_served(settings):
    store, run = prepared_store(settings)
    record, _ = store.publish_closing(run['run_id'], plan_closing)
    with sqlite3.connect(settings.database) as connection:
        connection.execute('UPDATE closings SET record = ? WHERE closing_id = ?', ('{}', record['closing_id']))
    with pytest.raises(AuditError) as error:
        store.get_closing(run['run_id'], record['closing_id'])
    assert error.value.code == 'CLOSING_CORRUPTED'


def test_tampered_checksum_is_rejected_even_with_valid_json(settings):
    store, run = prepared_store(settings)
    record, _ = store.publish_closing(run['run_id'], plan_closing)
    with sqlite3.connect(settings.database) as connection:
        raw, = connection.execute('SELECT record FROM closings WHERE closing_id = ?',
                                  (record['closing_id'],)).fetchone()
        tampered = json.loads(raw)
        tampered['totals']['close_cents'] += 1
        connection.execute('UPDATE closings SET record = ? WHERE closing_id = ?',
                           (canonical(tampered), record['closing_id']))
    with pytest.raises(AuditError) as error:
        store.get_closing(run['run_id'], record['closing_id'])
    assert error.value.code == 'CLOSING_CORRUPTED'


def test_concurrent_publications_keep_one_revision_per_ledger(settings):
    store, run = prepared_store(settings)
    store.append_event(run['run_id'], appeal_payload(), next_event)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.publish_closing(run['run_id'], plan_closing), range(4)))
    assert sum(not reused for _, reused in results) == 1
    assert {record['closing_id'] for record, _ in results} == {results[0][0]['closing_id']}
    with sqlite3.connect(settings.database) as connection:
        assert connection.execute('SELECT COUNT(*) FROM closings').fetchone() == (1,)


def test_preview_is_stable_and_does_not_write(client, run):
    open_appeal(client, run)
    first = client.get(path(run, '/preview')).json()
    second = client.get(path(run, '/preview')).json()
    assert first['closing_id'] == second['closing_id']
    assert first['persisted'] is False and second['persisted'] is False
    listing = client.get(path(run)).json()
    assert listing['versions'] == [] and listing['latest'] is None
    published = client.post(path(run)).json()
    assert published['closing_id'] == first['closing_id']
    assert published['persisted'] is True
    replayed = client.get(path(run, '/preview')).json()
    assert (replayed['revision'], replayed['reused']) == (published['revision'], True)


def test_unknown_run_and_closing_are_reported(client, run):
    missing = client.get('/api/settlement/runs/' + '0' * 64 + '/closings')
    assert (missing.status_code, missing.json()['detail']['code']) == (404, 'RUN_NOT_FOUND')
    absent = client.get(path(run, '/' + '1' * 64))
    assert (absent.status_code, absent.json()['detail']['code']) == (404, 'CLOSING_NOT_FOUND')
