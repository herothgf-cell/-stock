"""Exercise real application callbacks without creating desktop windows."""
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from stockjournal.app import JournalApp
from stockjournal.store import JournalStore
from tests.test_domain import seed, trade
from tests.test_sync import ScriptedHTTP
from tests.test_workflow import preview


class Value:
    def __init__(self, value):
        self.value = value
    def get(self):
        return self.value
    def set(self, value):
        self.value = value


class AppGuardTests(unittest.TestCase):
    def test_new_preview_does_not_reuse_previous_import_account(self):
        app=self.app
        app.import_account=Value('Main');app.update_orders=Value(True)
        app.tabs=SimpleNamespace(select=lambda page:None);app.pages={'imports':'imports'}
        app.load_preview(preview())
        self.assertEqual(app.import_account.get(),'원본 계좌 유지')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        app = JournalApp.__new__(JournalApp)
        self.app = app
        app.directory = Path(self.temp.name)
        app.store = JournalStore(app.directory / "journal.sqlite3")
        self.addCleanup(app.store.close)
        app.state = seed()
        app.store.save(app.state)
        app.root = object()
        app.settings = {}
        app.quotes = {}
        app.busy = False
        app.smoke = True
        app.account = Value("Main")
        app.status = Value("")
        app.refresh = lambda: None
        app.render_preview = lambda: None

    def edit_callback(self, collection, existing=None):
        dialogs = []
        def capture(parent, title, fields, callback, help_text=""):
            dialogs.append((callback, {name: value for name, label, value, choices in fields}))
        with patch("stockjournal.app.FormDialog", side_effect=capture):
            self.app.edit(collection, existing)
        return dialogs[0]

    def buy(self):
        row = trade("buy", "BUY", 2, 10, sequence=1, lot_id="lot-original", source="broker-import", external_id="fill-100", memo="original memo")
        self.app.state["trades"]["buy"] = row
        self.app.store.save(self.app.state)
        return row

    def test_trade_edit_preserves_import_identity_and_lot_metadata(self):
        callback, values = self.edit_callback("trades", self.buy())
        values.update(memo="reviewed memo", fee="2")
        callback(values)
        saved = self.app.store.load()["trades"]["buy"]
        self.assertEqual(saved["fee"], "2")
        self.assertEqual(saved["memo"], "reviewed memo")
        self.assertEqual((saved["source"], saved["external_id"], saved["lot_id"]), ("broker-import", "fill-100", "lot-original"))

    def test_stale_editor_cannot_overwrite_a_changed_record(self):
        callback, values = self.edit_callback("trades", self.buy())
        self.app.state["trades"]["buy"]["memo"] = "newer edit"
        self.app.store.save(self.app.state)
        values["memo"] = "stale edit"
        with self.assertRaises(ValueError):
            callback(values)
        self.assertEqual(self.app.store.load()["trades"]["buy"]["memo"], "newer edit")

    def test_trade_editor_retains_unrelated_changes_made_after_opening(self):
        callback, values = self.edit_callback("trades", self.buy())
        self.app.state["accounts"]["a"]["name"] = "Renamed while editor open"
        self.app.store.save(self.app.state)
        values["memo"] = "trade edit"
        callback(values)
        state = self.app.store.load()
        self.assertEqual(state["accounts"]["a"]["name"], "Renamed while editor open")
        self.assertEqual(state["trades"]["buy"]["memo"], "trade edit")

    def test_new_instrument_form_cannot_replace_an_existing_instrument(self):
        callback, values = self.edit_callback("instruments")
        values.update(market="US", code="ABC", name="Replacement", currency="USD")
        with self.assertRaises(ValueError):
            callback(values)
        self.assertEqual(self.app.store.load()["instruments"]["US:ABC"]["name"], "Example")

    def test_import_confirmation_cannot_replace_intervening_changes(self):
        app = self.app
        app.preview, app.chosen = preview(), {"buy", "sell"}
        app.import_account, app.include_events, app.update_orders = Value("원본 계좌 유지"), Value(True), Value(False)
        def concurrent_change(*args, **kwargs):
            app.state["accounts"]["a"]["name"] = "Newer account name"
            app.store.save(app.state)
            return True
        with patch("stockjournal.app.messagebox.askyesno", side_effect=concurrent_change), self.assertRaises(ValueError):
            app.commit_import()
        state = app.store.load()
        self.assertEqual(state["accounts"]["a"]["name"], "Newer account name")
        self.assertEqual(state["trades"], {})

    def test_missing_selected_backup_does_not_restore_an_empty_journal(self):
        before = self.app.store.load()
        with patch("stockjournal.app.filedialog.askopenfilename", return_value=str(self.app.directory / "missing.json")), patch("stockjournal.app.messagebox.askyesno", return_value=True):
            with self.assertRaises((FileNotFoundError, ValueError)):
                self.app.restore_backup()
        self.assertEqual(self.app.store.load(), before)

    def test_first_sync_initializes_remote_and_persists_the_new_baseline(self):
        app = self.app
        app.settings.update(sync_repo="owner/journal", sync_branch="main")
        app.secrets = SimpleNamespace(get=lambda key: "test-token")
        app.work = lambda label, function, done, error=None: done(function())
        missing = HTTPError("https://api.github.com", 404, "Not Found", {}, io.BytesIO(b""))
        empty = HTTPError("https://api.github.com", 409, "Empty", {}, io.BytesIO(b""))
        transport = ScriptedHTTP(self, [
            ("GET", "/repos/owner/journal", {"private": True, "default_branch": "main"}),
            ("GET", "/contents/journal.json?ref=main", missing),
            ("GET", "/branches/main", missing),
            ("GET", "/git/ref/heads/main", empty),
            ("GET", "/repos/owner/journal", {"private": True, "default_branch": "main"}),
            ("PUT", "/contents/journal.json", {"content": {"sha": "first-sha"}}),
        ])
        with patch("stockjournal.sync.urllib.request.build_opener", return_value=transport):
            app.sync()
        baseline = json.loads((app.directory / "sync-base.json").read_text())
        self.assertEqual(baseline["identity"], "owner/journal@main")
        self.assertEqual(baseline["sha"], "first-sha")
        self.assertEqual(baseline["state"], app.store.load())
        self.assertFalse(app.settings["dirty"])


if __name__ == "__main__":
    unittest.main()
