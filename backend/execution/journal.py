"""Single-writer SQLite execution journal. No broker credentials are stored."""
import fcntl
import json
import os
import sqlite3
from pathlib import Path


class ExecutionJournal:
    def __init__(self, path=':memory:'):
        self.durable = str(path) != ':memory:'
        self._lock = None
        if self.durable:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
            self._lock = os.fdopen(fd, 'a+')
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except Exception:
                self._lock.close()
                raise RuntimeError('Execution journal already owned by another process') from None
            fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
        try:
            self.db = sqlite3.connect(str(path))
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, state TEXT NOT NULL)')
            self.db.commit()
        except Exception:
            if hasattr(self, "db"):
                self.db.close()
            if self._lock:
                self._lock.close()
            raise

    def load(self):
        row = self.db.execute('SELECT state FROM events ORDER BY seq DESC LIMIT 1').fetchone()
        return json.loads(row[0]) if row else None

    def save(self, state):
        with self.db:
            self.db.execute('INSERT INTO events(state) VALUES (?)',
                            (json.dumps(state, sort_keys=True, allow_nan=False),))

    def close(self):
        self.db.close()
        if self._lock:
            self._lock.close()
