import copy
from types import SimpleNamespace
import unittest

from stockjournal import workflow
from stockjournal.domain import empty_state


STAMP = "2026-01-01T00:00:00Z"


def record(id, **fields):
    return {"id": id, "updated_at": STAMP, "deleted": False, **fields}


def preview():
    return SimpleNamespace(
        accounts=[record("a", name="Main", broker="Broker", kind="taxable", currency="USD")],
        instruments=[record("US:ABC", market="US", code="ABC", name="Example", currency="USD", asset_type="stock", sector="")],
        trades=[record("buy", account_id="a", instrument_id="US:ABC", executed_at="2026-01-01", side="BUY", quantity="2", price="10", fee="0", tax="0", currency="USD", source="xlsx:매매일지", lot_id="pair", sequence=1),
                record("sell", account_id="a", instrument_id="US:ABC", executed_at="2026-01-02", side="SELL", quantity="2", price="12", fee="0", tax="0", currency="USD", source="xlsx:매매일지", lot_id="pair", sequence=2)],
        dividends=[], dividend_events=[], warnings=[])


class WorkflowTests(unittest.TestCase):
    def test_partial_legacy_pair_is_rejected_without_mutating_state(self):
        state = empty_state()
        snapshot = copy.deepcopy(state)
        with self.assertRaises(ValueError):
            workflow.stage_import(state, preview(), selected={"sell"})
        self.assertEqual(state, snapshot)

    def test_existing_purchase_and_later_completed_pair_adds_only_sale(self):
        initial = preview()
        initial.trades = initial.trades[:1]
        state, _ = workflow.stage_import(empty_state(), initial)
        merged, stats = workflow.stage_import(state, preview())
        self.assertEqual(set(merged["trades"]), {"buy", "sell"})
        self.assertEqual(stats["added"], 1)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(set(state["trades"]), {"buy"})

    def test_duplicate_record_ids_are_rejected(self):
        p = preview()
        p.trades.append(copy.deepcopy(p.trades[0]))
        with self.assertRaises(ValueError):
            workflow.stage_import(empty_state(), p)

    def test_conflicting_instrument_currency_blocks_import(self):
        p = preview()
        state = empty_state()
        state["instruments"]["US:ABC"] = {**p.instruments[0], "currency": "JPY"}
        with self.assertRaises(ValueError):
            workflow.stage_import(state, p)
        self.assertEqual(state["trades"], {})

    def test_repeated_import_skips_records_and_cannot_resurrect_tombstones(self):
        state, _ = workflow.stage_import(empty_state(), preview())
        repeated, stats = workflow.stage_import(state, preview())
        self.assertEqual(stats["added"], 0)
        self.assertEqual(stats["skipped"], 2)
        self.assertEqual(repeated, state)
        for row in state["trades"].values():
            row["deleted"] = True
        repeated, stats = workflow.stage_import(state, preview(), update_existing=True)
        self.assertEqual(stats["added"], 0)
        self.assertTrue(all(row["deleted"] for row in repeated["trades"].values()))

    def test_unchanged_rejects_edit_or_delete_since_editor_opened(self):
        state, _ = workflow.stage_import(empty_state(), preview())
        opened = copy.deepcopy(state["trades"]["buy"])
        workflow.unchanged(state, "trades", opened)
        state["trades"]["buy"]["price"] = "99"
        with self.assertRaises(ValueError):
            workflow.unchanged(state, "trades", opened)
        del state["trades"]["buy"]
        with self.assertRaises(ValueError):
            workflow.unchanged(state, "trades", opened)

    def test_ambiguous_account_names_require_explicit_override(self):
        state = empty_state()
        state["accounts"] = {
            "one": record("one", name="Main", broker="Broker", kind="taxable", currency="USD"),
            "two": record("two", name="Main", broker="Broker", kind="taxable", currency="USD"),
        }
        with self.assertRaises(ValueError):
            workflow.stage_import(state, preview())
        merged, _ = workflow.stage_import(state, preview(), account_override="two")
        self.assertTrue(all(row["account_id"] == "two" for row in merged["trades"].values()))

    def test_minimal_instrument_tombstone_is_controlled_rejection(self):
        state = empty_state()
        state["instruments"]["US:ABC"] = {"id": "US:ABC", "updated_at": STAMP, "deleted": True}
        with self.assertRaises(ValueError):
            workflow.stage_import(state, preview())


if __name__ == "__main__":
    unittest.main()
