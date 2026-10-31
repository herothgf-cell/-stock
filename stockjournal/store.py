"""Atomic SQLite journal persistence and consistent SQLite backups."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .domain import empty_state, validate_state


class StoreError(RuntimeError):
    pass


class JournalStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=10)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        with self._connection:
            self._connection.execute("CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL)")

    def load(self):
        try:
            row = self._connection.execute("SELECT payload FROM journal WHERE id = 1").fetchone()
            state = empty_state() if row is None else json.loads(row[0])
            validate_state(state)
            return state
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            raise StoreError("Unable to read the local journal database") from exc

    def save(self, state):
        validate_state(state)
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        try:
            with self._connection:
                self._connection.execute("INSERT INTO journal (id, payload) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET payload = excluded.payload", (payload,))
        except sqlite3.Error as exc:
            raise StoreError("Unable to save the local journal database") from exc

    def backup(self, path):
        target = Path(path)
        if target.resolve() == self.path.resolve():
            raise StoreError("Backup destination must differ from the live database")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(sqlite3.connect(target)) as destination:
                self._connection.backup(destination)
            return target
        except sqlite3.Error as exc:
            raise StoreError("Unable to back up the local journal database") from exc

    def close(self):
        self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
