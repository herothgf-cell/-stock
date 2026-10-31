import importlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class StoreTests(unittest.TestCase):
    def setUp(self):
        try:
            self.d = importlib.import_module("stockjournal.domain")
            self.m = importlib.import_module("stockjournal.store")
        except ModuleNotFoundError:
            self.fail("The journal storage implementation is missing")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "journal.sqlite3"

    def test_round_trip_is_detached_and_survives_reopening(self):
        with self.m.JournalStore(self.path) as store:
            s = store.load()
            s["accounts"]["a"] = {"id": "a", "updated_at": "2026-01-01T00:00:00Z", "deleted": False, "name": "Main", "broker": "Broker", "kind": "taxable", "currency": "USD"}
            store.save(s)
            s["accounts"]["a"]["name"] = "Unsaved"
            self.assertEqual(store.load()["accounts"]["a"]["name"], "Main")
        with self.m.JournalStore(self.path) as store:
            self.assertEqual(store.load()["accounts"]["a"]["name"], "Main")

    def test_invalid_write_leaves_last_committed_state(self):
        with self.m.JournalStore(self.path) as store:
            before = store.load()
            with self.assertRaises(ValueError):
                store.save({**before, "token": "secret"})
            self.assertEqual(store.load(), before)

    def test_sqlite_backup_is_reopenable(self):
        target = Path(self.temp.name) / "backup.sqlite3"
        with self.m.JournalStore(self.path) as store:
            store.save(store.load())
            store.backup(target)
        with self.m.JournalStore(target) as restored:
            self.assertEqual(restored.load(), self.d.empty_state())

    def test_backup_closes_destination_connection_for_windows_file_access(self):
        target = Path(self.temp.name) / "backup.sqlite3"
        with self.m.JournalStore(self.path) as store:
            connection = sqlite3.connect(target)
            self.addCleanup(connection.close)
            with patch("stockjournal.store.sqlite3.connect", return_value=connection):
                store.backup(target)
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
