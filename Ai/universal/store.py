"""Small transactional store. The v0.1 host uses one process and one SQLite writer."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import threading


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.touch(mode=0o600, exist_ok=True)
            if os.name != "nt":
                path.chmod(0o600)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS objects (
                kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(kind, id));
            CREATE TABLE IF NOT EXISTS audit (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL,
                principal_id TEXT NOT NULL, operation TEXT NOT NULL,
                resource TEXT NOT NULL, outcome TEXT NOT NULL);
        """)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def get(self, kind, key):
        row = self.db.execute("SELECT data FROM objects WHERE kind=? AND id=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind, key, value):
        self.db.execute("INSERT INTO objects VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
                        (kind, key, json.dumps(value, allow_nan=False)))

    def all(self, kind):
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM objects WHERE kind=? ORDER BY id", (kind,))]

    def delete(self, kind, key):
        self.db.execute("DELETE FROM objects WHERE kind=? AND id=?", (kind, key))

    def close(self):
        with self.lock:
            self.db.close()
