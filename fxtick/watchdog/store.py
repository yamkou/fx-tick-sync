"""Explicit SQLite monitor state, isolated from market-data files/registries.

Single monitor writer. State transitions and outbox enqueue share a transaction.
No network sends occur inside a transaction. Backups/retention are operator work.
"""
import json
import sqlite3
from contextlib import contextmanager


class SQLiteState:
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
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

    def incident(self, key):
        row = self.connection.execute('SELECT state FROM monitor_incidents WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def pending(self):
        return self.connection.execute('SELECT * FROM monitor_outbox WHERE delivered=0 ORDER BY number').fetchall()
