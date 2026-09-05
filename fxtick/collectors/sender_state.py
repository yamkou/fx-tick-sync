"""Durable send reservations and synthetic write evidence; no market-data output."""
from pathlib import Path
import sqlite3

from ..config import ConfigError, logical_id


class SenderState:
    def __init__(self, directory, collector_id, boot_id):
        logical_id(collector_id); logical_id(boot_id)
        self.db = self.lease = None
        directory = Path(directory)
        if not directory.is_dir() or directory.resolve() != directory:
            raise ConfigError('Sender state directory is unavailable')
        for name in ('sender.sqlite', 'sender.lock.sqlite'):
            p = directory/name
            if p.is_symlink() or p.resolve() != p:
                raise ConfigError('Sender state must not redirect')
        try:
            # Separate SQLite exclusive lock remains held for the process lifetime;
            # sequence DB commits do not release this single-owner guard.
            self.lease = sqlite3.connect(directory/'sender.lock.sqlite', timeout=0)
            self.lease.execute('BEGIN EXCLUSIVE')
            self.db = sqlite3.connect(directory/'sender.sqlite', timeout=5)
            self.db.execute('PRAGMA synchronous=FULL')
            if self.db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise ConfigError('Sender state integrity failed')
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if version == 0 and not tables:
                with self.db:
                    self.db.execute('CREATE TABLE sender (collector TEXT, boot TEXT, sequence INTEGER NOT NULL)')
                    self.db.execute('INSERT INTO sender VALUES (?,?,0)', (collector_id, boot_id))
                    self.db.execute('CREATE TABLE synthetic_write (singleton INTEGER PRIMARY KEY CHECK(singleton=1), observed_at TEXT NOT NULL)')
                    self.db.execute('PRAGMA user_version=1')
            elif version != 1:
                raise ConfigError('Unsupported sender schema; no automatic reset')
            rows = self.db.execute('SELECT collector,boot,sequence FROM sender').fetchall()
            if (len(rows) != 1 or rows[0][:2] != (collector_id,boot_id)
                    or type(rows[0][2]) is not int or not 0 <= rows[0][2] < 2**63):
                raise ConfigError('Sender identity or sequence mismatch; no automatic reset')
        except Exception:
            self.close()
            raise ConfigError('Sender state unavailable; details omitted') from None

    def reserve(self):
        with self.db:
            value = self.db.execute('SELECT sequence FROM sender').fetchone()[0] + 1
            if value >= 2**63: raise ConfigError('Sender sequence exhausted')
            self.db.execute('UPDATE sender SET sequence=?', (value,))
        return value  # Durable before any transport call; crash may leave safe gaps.

    def synthetic_write(self, now):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO synthetic_write VALUES (1,?)', (now.isoformat(),))

    def close(self):
        if self.db is not None:
            self.db.close(); self.db = None
        if self.lease is not None:
            self.lease.close(); self.lease = None
