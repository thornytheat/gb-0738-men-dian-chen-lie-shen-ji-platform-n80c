import json
import sqlite3
from contextlib import contextmanager
from hashlib import sha256

from app.errors import AuditError
from app.manifest import canonical

DDL = (
    '''CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS appeal_events (
        seq INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        shelf_key TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0),
        request_id TEXT NOT NULL,
        payload TEXT NOT NULL,
        event TEXT NOT NULL,
        UNIQUE (run_id, shelf_key, version),
        UNIQUE (run_id, request_id)
    )''',
    '''CREATE TABLE IF NOT EXISTS closings (
        seq INTEGER PRIMARY KEY,
        closing_id TEXT NOT NULL UNIQUE,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        ledger TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision > 0),
        state TEXT NOT NULL CHECK (state = 'PUBLISHED'),
        checksum TEXT NOT NULL,
        record TEXT NOT NULL
    )''',
    '''CREATE INDEX IF NOT EXISTS closings_by_run ON closings (run_id, state, revision)''',
)


class Store:
    def __init__(self, database):
        self.database = database

    @contextmanager
    def connection(self, write=False):
        connection = sqlite3.connect(self.database, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA foreign_keys = ON')
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connection(write=True) as connection:
            for statement in DDL:
                connection.execute(statement)

    @staticmethod
    def read(connection, run_id, required=True):
        found = connection.execute('SELECT snapshot FROM runs WHERE run_id = ?', (run_id,)).fetchone()
        if found is None:
            if required:
                raise AuditError('RUN_NOT_FOUND', '未找到该巡检批次', 404)
            return None
        events = connection.execute('SELECT event FROM appeal_events WHERE run_id = ? ORDER BY seq', (run_id,))
        return {**json.loads(found['snapshot']), 'events': [json.loads(event['event']) for event in events]}

    def get(self, run_id, required=True):
        with self.connection() as connection:
            return self.read(connection, run_id, required)

    def save(self, snapshot):
        run_id = snapshot['manifest']['run_id']
        record = canonical(snapshot)
        with self.connection(write=True) as connection:
            inserted = connection.execute('INSERT INTO runs VALUES (?, ?) ON CONFLICT(run_id) DO NOTHING', (run_id, record))
            return self.read(connection, run_id), inserted.rowcount == 0

    def append_event(self, run_id, payload, build):
        with self.connection(write=True) as connection:
            snapshot = self.read(connection, run_id)
            raw = canonical(payload)
            old = connection.execute('SELECT payload, event FROM appeal_events WHERE run_id = ? AND request_id = ?',
                                     (run_id, payload['request_id'])).fetchone()
            if old:
                if old['payload'] != raw:
                    raise AuditError('REQUEST_CONFLICT', '提交编号已用于其他内容', 409)
                return json.loads(old['event']), True
            event = build(snapshot, payload)
            connection.execute('''INSERT INTO appeal_events
                (run_id, shelf_key, version, request_id, payload, event) VALUES (?, ?, ?, ?, ?, ?)''',
                (run_id, event['key'], event['version'], payload['request_id'], raw, canonical(event)))
            return event, False

    def closing_history(self, connection, run_id):
        found = connection.execute(
            'SELECT record, checksum, state, revision FROM closings WHERE run_id = ? AND state = ? ORDER BY revision, seq',
            (run_id, 'PUBLISHED'))
        return [self._decode_closing(row) for row in found]

    def read_closings(self, run_id):
        with self.connection() as connection:
            self.read(connection, run_id)
            return {'published': self.closing_history(connection, run_id),
                    'latest': self.latest_closing(connection, run_id)}

    def latest_closing(self, connection, run_id):
        found = connection.execute(
            '''SELECT record, checksum, state, revision FROM closings WHERE run_id = ?
               ORDER BY revision DESC, seq DESC LIMIT 1''', (run_id,)).fetchone()
        if found is None:
            return None
        return self._decode_closing(found)

    def get_closing(self, run_id, closing_id):
        with self.connection() as connection:
            self.read(connection, run_id)
            found = connection.execute(
                'SELECT record, checksum, state, revision FROM closings WHERE run_id = ? AND closing_id = ?',
                (run_id, closing_id)).fetchone()
            if found is None:
                raise AuditError('CLOSING_NOT_FOUND', '未找到该版结转记录', 404)
            return self._decode_closing(found)

    @staticmethod
    def _decode_closing(found):
        raw = found['record']
        if sha256(raw.encode()).hexdigest() != found['checksum']:
            raise AuditError('CLOSING_CORRUPTED', '结转记录校验失败，不予出具', 500)
        try:
            record = json.loads(raw)
        except ValueError as error:
            raise AuditError('CLOSING_CORRUPTED', '结转记录无法解析，不予出具', 500) from error
        if not isinstance(record, dict) or 'closing_id' not in record or 'totals' not in record:
            raise AuditError('CLOSING_CORRUPTED', '结转记录结构不完整，不予出具', 500)
        return {**record, 'state': found['state'], 'revision': found['revision']}

    def publish_closing(self, run_id, build):
        """在单个写事务内读取最新异议台账并追加一版结转；失败整版回滚，旧版保持可读。"""
        with self.connection(write=True) as connection:
            snapshot = self.read(connection, run_id)
            current = self.latest_closing(connection, run_id)
            published = self.closing_history(connection, run_id)
            record, reused = build(snapshot, current, published)
            if reused:
                return record, True
            stored = canonical({**record, 'persisted': True, 'reused': None})
            connection.execute(
                'INSERT INTO closings (closing_id, run_id, ledger, revision, state, checksum, record) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (record['closing_id'], run_id, record['basis']['ledger'], record['revision'], 'PUBLISHED',
                 sha256(stored.encode()).hexdigest(), stored))
            written = self.latest_closing(connection, run_id)
            if written is None or written['closing_id'] != record['closing_id']:
                raise AuditError('CLOSING_WRITE_FAILED', '结转记录未能完整落盘，已恢复旧版', 500)
            return {**written, 'reused': False}, False
