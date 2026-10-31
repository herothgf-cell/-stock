"""Hand-calculated accounting and validation regression tests."""
import copy
import importlib
import unittest
from decimal import Decimal, localcontext


STAMP = "2026-01-01T00:00:00+00:00"


def record(id, **fields):
    return {"id": id, "updated_at": STAMP, "deleted": False, **fields}


def seed():
    return {
        "schema_version": 1,
        "accounts": {"a": record("a", name="Main", broker="Broker", kind="taxable", currency="USD")},
        "instruments": {"US:ABC": record("US:ABC", market="US", code="ABC", name="Example", currency="USD", asset_type="stock", sector="Technology")},
        "trades": {}, "dividends": {}, "dividend_events": {},
    }


def trade(id, side, quantity, price, **fields):
    return record(id, account_id="a", instrument_id="US:ABC", executed_at="2026-01-02", side=side,
                  quantity=str(quantity), price=str(price), fee="0", tax="0", currency="USD", **fields)


class DomainTests(unittest.TestCase):
    def setUp(self):
        try:
            self.d = importlib.import_module("stockjournal.domain")
        except ModuleNotFoundError:
            self.fail("The financial core implementation is missing")
        self.s = seed()

    def row(self, quotes=None):
        return self.d.portfolio(self.s, quotes or {})["rows"][0]

    def test_average_cost_and_sell_charges(self):
        self.s["trades"] = {
            "b1": {**trade("b1", "BUY", 10, 10, sequence=1), "fee": "1", "tax": "1"},
            "b2": trade("b2", "BUY", 10, 20, sequence=2),
            "s1": {**trade("s1", "SELL", 5, 30, sequence=3), "fee": "2", "tax": "3"},
        }
        r = self.row({"US:ABC": "25"})
        self.assertEqual(r["quantity"], Decimal("15"))
        self.assertEqual(r["cost"], Decimal("226.5"))
        self.assertEqual(r["average_cost"], Decimal("15.1"))
        self.assertEqual(r["realized_pnl"], Decimal("69.5"))
        self.assertEqual(r["unrealized_pnl"], Decimal("148.5"))
        self.assertEqual(r["total_pnl"], Decimal("218"))

    def test_specific_lot_and_proportional_remaining_lots(self):
        self.s["trades"] = {
            "b1": trade("b1", "BUY", 10, 10, sequence=1, lot_id="cheap"),
            "b2": trade("b2", "BUY", 10, 20, sequence=2, lot_id="dear"),
            "s1": trade("s1", "SELL", 10, 30, sequence=3, lot_id="cheap"),
            "s2": trade("s2", "SELL", 5, 30, sequence=4),
        }
        r = self.row({"US:ABC": "30"})
        self.assertEqual(r["cost"], Decimal("100"))
        self.assertEqual(r["average_cost"], Decimal("20"))
        self.assertEqual(r["realized_pnl"], Decimal("250"))

    def test_numeric_same_day_sequence_prevents_lexical_oversell(self):
        self.s["trades"] = {"z": trade("z", "BUY", 1, 10, sequence=2), "a": trade("a", "SELL", 1, 11, sequence=10)}
        self.d.validate_state(self.s)
        self.assertEqual(self.row()["realized_pnl"], Decimal("1"))

    def test_oversell_and_specific_lot_oversell_are_rejected(self):
        self.s["trades"] = {"b": trade("b", "BUY", 1, 10, sequence=1, lot_id="one"), "s": trade("s", "SELL", 2, 11, sequence=2)}
        with self.assertRaises(self.d.ValidationError):
            self.d.validate_state(self.s)
        self.s["trades"]["x"] = trade("x", "BUY", 10, 10, sequence=0, lot_id="two")
        self.s["trades"]["s"]["lot_id"] = "one"
        with self.assertRaises(self.d.ValidationError):
            self.d.validate_state(self.s)

    def test_unpriced_holdings_propagate_to_currency_totals(self):
        self.s["trades"] = {"b": trade("b", "BUY", 3, 10)}
        p = self.d.portfolio(self.s)
        self.assertIsNone(p["rows"][0]["market_value"])
        self.assertIsNone(p["totals"]["USD"]["total_pnl"])
        self.assertEqual(p["totals"]["USD"]["unpriced_count"], 1)
        self.assertEqual(p["totals"]["USD"]["cost"], Decimal("30"))

    def test_sold_out_and_dividend_only_positions_are_retained(self):
        self.s["trades"] = {"b": trade("b", "OPENING", 1, 10, sequence=1), "s": trade("s", "SELL", 1, 12, sequence=2)}
        self.s["dividends"]["d"] = record("d", account_id="a", instrument_id="US:ABC", paid_date="2026-02-01", gross="4", tax="1", currency="USD")
        r = self.row()
        self.assertEqual((r["quantity"], r["market_value"], r["total_pnl"]), (Decimal(0), Decimal(0), Decimal(5)))
        self.s["trades"] = {}
        self.assertEqual(self.row()["total_pnl"], Decimal(3))

    def test_accounts_and_currencies_stay_separate(self):
        self.s["accounts"]["j"] = record("j", name="Japan", broker="B", kind="nisa", currency="JPY")
        self.s["instruments"]["JP:1234"] = record("JP:1234", market="JP", code="1234", name="Japan Co", currency="JPY", asset_type="stock", sector="")
        self.s["trades"] = {"a": trade("a", "BUY", 1, 10), "j": {**trade("j", "BUY", 1, 100), "account_id": "j", "instrument_id": "JP:1234", "currency": "JPY"}}
        p = self.d.portfolio(self.s, {"US:ABC": "12", "JP:1234": "110"})
        self.assertEqual(set(p["totals"]), {"USD", "JPY"})
        self.assertEqual(p["totals"]["JPY"]["total_pnl"], Decimal("10"))
        self.assertEqual(len(self.d.portfolio(self.s, account_id="a")["rows"]), 1)

    def test_decimal_precision_does_not_depend_on_caller_context(self):
        self.s["trades"] = {"b": trade("b", "BUY", "3", "0.123456789123456789123456789123456789")}
        with localcontext() as ctx:
            ctx.prec = 6
            r = self.row({"US:ABC": "1"})
        self.assertEqual(r["cost"], Decimal("0.370370367370370367370370367370370367"))

    def test_rejects_wrong_types_missing_references_and_currency(self):
        for patch in ({"quantity": 1}, {"quantity": "NaN"}, {"price": "-1"}, {"sequence": True}, {"account_id": "missing"}, {"currency": "JPY"}):
            with self.subTest(patch=patch):
                self.s["trades"] = {"b": {**trade("b", "BUY", 1, 10), **patch}}
                with self.assertRaises(self.d.ValidationError):
                    self.d.validate_state(self.s)

    def test_tombstones_preserve_identity_without_affecting_ledger(self):
        self.s["trades"]["old"] = {"id": "old", "updated_at": STAMP, "deleted": True}
        self.d.validate_state(self.s)
        self.assertEqual(self.d.portfolio(self.s)["rows"], [])

    def test_rejects_secret_fields_and_extra_top_level_schema(self):
        for mutate in (lambda s: s.update(token="private"), lambda s: s["accounts"]["a"].update(access_token="private")):
            s = copy.deepcopy(self.s)
            mutate(s)
            with self.assertRaises(self.d.ValidationError):
                self.d.validate_state(s)

    def test_announcement_is_not_paid_dividend_income(self):
        self.s["dividend_events"]["e"] = record("e", instrument_id="US:ABC", amount_per_share="9", currency="USD", status="reference", source="official", record_date="2026-02-01", pay_date="2026-03-01")
        self.assertEqual(self.d.portfolio(self.s)["rows"], [])

    def test_full_exit_after_proportional_sale_has_no_phantom_holding(self):
        self.s["trades"] = {str(i): trade(str(i), "BUY", quantity, i + 1, sequence=i) for i, quantity in enumerate((1, 2, 3))}
        self.s["trades"]["s1"] = trade("s1", "SELL", 1, 10, sequence=10)
        self.s["trades"]["s2"] = trade("s2", "SELL", 5, 10, sequence=11)
        r = self.row()
        self.assertEqual(r["quantity"], Decimal(0))
        self.assertEqual(r["cost"], Decimal(0))
        self.assertEqual(r["total_pnl"], Decimal(46))

    def test_instrument_identifier_is_not_mistaken_for_a_secret_field(self):
        instrument = self.s["instruments"].pop("US:ABC")
        instrument.update(id="US:TOKEN", code="TOKEN")
        self.s["instruments"]["US:TOKEN"] = instrument
        self.d.validate_state(self.s)


if __name__ == "__main__":
    unittest.main()
