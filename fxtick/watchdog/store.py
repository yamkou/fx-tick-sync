"""Explicit SQLite monitor state, isolated from market-data files/registries.

Single monitor writer. State transitions and outbox enqueue share a transaction.
No network sends occur inside a transaction. Backups/retention are operator work.
"""
import json
import sqlite3
from datetime import datetime
from contextlib import contextmanager

from ..collectors.health import HeartbeatReceipt
from ..config import ConfigError
from .heartbeat import Heartbeat


class SQLiteState:
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        tables = {r[0] for r in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        expected = {'monitor_meta', 'monitor_nodes', 'monitor_boots', 'monitor_incidents', 'monitor_outbox'}
        application_id = self.connection.execute('PRAGMA application_id').fetchone()[0]
        version = self.connection.execute('PRAGMA user_version').fetchone()[0]
        if (tables and tables != expected) or application_id not in (0, 0x4658544D) or version not in (0, 1):
            self.connection.close()
            raise ConfigError('Use a dedicated compatible monitor state database')
        self.connection.executescript('''
            CREATE TABLE IF NOT EXISTS monitor_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS monitor_nodes (id TEXT PRIMARY KEY, enrolled TEXT NOT NULL,
                receipt TEXT, payload TEXT, boot TEXT, sequence INTEGER);
            CREATE TABLE IF NOT EXISTS monitor_boots (node TEXT NOT NULL, boot TEXT NOT NULL,
                PRIMARY KEY(node, boot));
            CREATE TABLE IF NOT EXISTS monitor_incidents (key TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS monitor_outbox (number INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL, route TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
                last_attempt TEXT, UNIQUE(event, route));
        ''')
        self.connection.execute('PRAGMA application_id = 1180193869')
        self.connection.execute('PRAGMA user_version = 1')
        self.connection.commit()

    @contextmanager
    def transaction(self):
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            yield self.connection
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self):
        self.connection.close()

    def node(self, collector_id):
        return self.connection.execute('SELECT * FROM monitor_nodes WHERE id=?', (collector_id,)).fetchone()

    def latest(self, collector_id):
        """Phase 3A HeartbeatStore contract: return receiver-owned receipt."""
        row = self.node(collector_id)
        if row is None or row['receipt'] is None:
            return None
        monitor = self.connection.execute("SELECT value FROM monitor_meta WHERE key='monitor-id'").fetchone()[0]
        heartbeat = Heartbeat.decode(row['payload'].encode('utf-8'))
        return HeartbeatReceipt(monitor, datetime.fromisoformat(row['receipt']),
            heartbeat.snapshot, heartbeat.boot_id, heartbeat.sequence)

    def incident(self, key):
        row = self.connection.execute('SELECT state FROM monitor_incidents WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def pending(self):
        return self.connection.execute('SELECT * FROM monitor_outbox WHERE delivered=0 ORDER BY number').fetchall()
