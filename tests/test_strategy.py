"""Strategy tests use synthetic closes and hand-calculated quantities only."""
import copy
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext

from stockjournal.domain import empty_state


NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
STAMP = "2026-10-01T00:00:00Z"
FIXTURE_HOLIDAYS = {"2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"}


def record(uid, **fields):
    return {"id": uid, "updated_at": STAMP, "deleted": False, **fields}


def seed(quantity="0", spym="0"):
    state = empty_state()
    for account in ("a", "b"):
        state["accounts"][account] = record(account, name=account, broker="Synthetic", kind="taxable", currency="USD")
    for symbol in ("TQQQ", "SPYM", "SGOV"):
        uid = "US:" + symbol
        state["instruments"][uid] = record(uid, market="US", code=symbol, name=symbol, asset_type="ETF", sector="", currency="USD")
    for symbol, amount in (("TQQQ", quantity), ("SPYM", spym)):
        if Decimal(amount):
            uid = "opening-" + symbol
            state["trades"][uid] = trade(uid, "BUY", amount, "50", symbol=symbol, executed_at="2026-09-01")
    return state


def trade(uid, side, quantity, price="110", symbol="TQQQ", **changes):
    return record(uid, **{"account_id": "a", "instrument_id": "US:" + symbol, "side": side, "quantity": quantity,
                          "price": price, "fee": "0", "tax": "0", "currency": "USD", "executed_at": "2026-10-26T21:00:00Z", **changes})


def history(values=None, end="2026-10-26", **changes):
    values = values or ["100"] * 199 + ["90", "110"]
    days = []
    day = date.fromisoformat(end)
    while len(days) < len(values):
        if day.weekday() < 5 and day.isoformat() not in FIXTURE_HOLIDAYS:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    rows = [{"date": day, "close": str(value)} for day, value in zip(reversed(days), values)]
    return {"symbol": "TQQQ", "currency": "USD", "session": "regular", "adjustment": "split_adjusted",
            "finalization": "completed_sessions", "as_of": NOW.isoformat(), "source": "synthetic", "rows": rows, **changes}


class StrategyTests(unittest.TestCase):
    def setUp(self):
        try:
            self.s = importlib.import_module("stockjournal.strategy")
        except ModuleNotFoundError:
            self.fail("The read-only strategy engine is not implemented")

    def active_cycle(self, quantity="100", spym="0", levels=()):
        state = seed(quantity, spym)
        return self.s.adopt_cycle(state, "a", start_date="2026-09-01", entry_count=3, profit_levels=levels, now=NOW)

    def test_201_closes_are_required_for_previous_and_current_average(self):
        decision = self.s.evaluate(seed(), "a", history(["100"] * 200), cash_usd="900", now=NOW)
        self.assertEqual(decision.status, "blocked")
        self.assertFalse(decision.actions)
        self.assertTrue(any("201" in item for item in decision.blockers))

    def test_upcross_budgets_one_third_and_floors_whole_share_buy(self):
        state = seed()
        before = copy.deepcopy(state)
        with localcontext() as context:
            context.prec = 6
            decision = self.s.evaluate(state, "a", history(), cash_usd="1000", now=NOW)
        self.assertEqual(decision.previous_sma, Decimal("99.95"))
        self.assertEqual(decision.sma, Decimal("100"))
        action = next(a for a in decision.actions if a.kind == "BUY_ENTRY")
        self.assertEqual(action.quantity, Decimal("3"))
        self.assertTrue(str(action.budget_usd).startswith("333.333333333333333333333333333333"))
        self.assertEqual(state, before)

    def test_above_average_without_cross_does_not_start_cycle(self):
        decision = self.s.evaluate(seed(), "a", history(["100"] * 199 + ["110", "111"]), cash_usd="900", now=NOW)
        self.assertFalse(any(a.kind == "BUY_ENTRY" for a in decision.actions))

    def test_equality_holds_and_exit_has_priority_over_profit(self):
        state = self.active_cycle(spym="5")
        equal = self.s.evaluate(state, "a", history(["100"] * 201), broker_return_pct="100", now=NOW)
        self.assertEqual(equal.status, "hold")
        self.assertFalse(equal.actions)
        below = self.s.evaluate(state, "a", history(["100"] * 200 + ["90"]), broker_return_pct="100", now=NOW)
        exits = {a.symbol: a.quantity for a in below.actions if a.kind == "EXIT"}
        self.assertEqual(exits, {"TQQQ": Decimal("100"), "SPYM": Decimal("5")})
        self.assertFalse(any(a.kind == "SELL_PROFIT" for a in below.actions))

    def test_unknown_adjustment_is_blocked_until_exact_series_review(self):
        state = seed()
        data = history(adjustment="unknown")
        self.assertEqual(self.s.evaluate(state, "a", data, cash_usd="900", now=NOW).status, "blocked")
        checked = self.s.confirm_history(state, "a", data, now=NOW)
        self.assertEqual(self.s.evaluate(checked, "a", data, cash_usd="900", now=NOW).status, "entry")
        changed = copy.deepcopy(data)
        changed["rows"][-1]["close"] = "111"
        self.assertEqual(self.s.evaluate(checked, "a", changed, cash_usd="900", now=NOW).status, "blocked")

    def test_stale_or_future_or_intraday_rows_cannot_produce_actions(self):
        for data in (history(end="2026-10-19"), history(end="2026-10-28"), history(end="2026-10-27")):
            with self.subTest(end=data["rows"][-1]["date"]):
                decision = self.s.evaluate(seed(), "a", data, cash_usd="900", now=NOW)
                self.assertEqual(decision.status, "blocked")
                self.assertFalse(decision.actions)

    def test_duplicate_dates_and_nonregular_data_are_rejected(self):
        duplicate = history()
        duplicate["rows"].append(dict(duplicate["rows"][-1]))
        for data in (duplicate, history(session="extended"), history(currency="KRW")):
            with self.subTest(data=data.get("session")):
                decision = self.s.evaluate(seed(), "a", data, cash_usd="900", now=NOW)
                self.assertEqual(decision.status, "blocked")

    def test_new_york_close_uses_dst_and_friday_for_weekends(self):
        cases = [
            ("2026-07-06T20:09:00+00:00", "2026-07-02"),
            ("2026-07-06T20:10:00+00:00", "2026-07-06"),
            ("2026-01-06T21:09:00+00:00", "2026-01-05"),
            ("2026-01-06T21:10:00+00:00", "2026-01-06"),
            ("2026-10-31T22:00:00+00:00", "2026-10-30"),
            ("2026-11-27T18:09:00+00:00", "2026-11-25"),
            ("2026-11-27T18:10:00+00:00", "2026-11-27"),
        ]
        for now, expected in cases:
            with self.subTest(now=now):
                self.assertEqual(self.s.completed_session_date(datetime.fromisoformat(now)).isoformat(), expected)

    def test_gap_profit_preview_uses_remaining_shares_and_half_up(self):
        state = self.active_cycle()
        decision = self.s.evaluate(state, "a", history(), broker_return_pct="100", now=NOW)
        profits = [a for a in decision.actions if a.kind == "SELL_PROFIT"]
        self.assertEqual([a.threshold_pct for a in profits], list(map(Decimal, ("10", "25", "50", "100"))))
        self.assertEqual([a.quantity for a in profits], list(map(Decimal, ("10", "9", "8", "37"))))
        self.assertTrue(decision.manual_review)

    def test_completed_levels_are_not_repeated_after_fall_and_recovery(self):
        state = self.active_cycle(quantity="73", levels=("10", "25", "50"))
        for roi in ("49", "99"):
            self.assertFalse(self.s.evaluate(state, "a", history(), broker_return_pct=roi, now=NOW).actions)
        decision = self.s.evaluate(state, "a", history(), broker_return_pct="200", now=NOW)
        self.assertEqual([a.quantity for a in decision.actions if a.kind == "SELL_PROFIT"], [Decimal("37"), Decimal("18")])

    def test_missing_fx_aware_return_does_not_trigger_usd_profit_signal(self):
        state = self.active_cycle()
        state["trades"]["opening-TQQQ"]["fee"] = "20"
        decision = self.s.evaluate(state, "a", history(), now=NOW)
        self.assertTrue(decision.usd_return_pct > Decimal("100"))
        self.assertEqual(decision.average_cost, Decimal("50.2"))
        self.assertFalse(any(a.kind == "SELL_PROFIT" for a in decision.actions))

    def test_low_share_and_fractional_rounding_never_oversell(self):
        whole = self.s.evaluate(self.active_cycle("1"), "a", history(), broker_return_pct="100", now=NOW)
        self.assertEqual([a.quantity for a in whole.actions if a.kind == "SELL_PROFIT"], [Decimal("0"), Decimal("0"), Decimal("0"), Decimal("1")])
        fractional = self.s.evaluate(self.active_cycle("0.75"), "a", history(), broker_return_pct="10", quantity_step="0.001", now=NOW)
        self.assertEqual(fractional.actions[0].quantity, Decimal("0.075"))

    def test_saved_broker_return_expires_on_new_close_or_holding_change(self):
        state = self.active_cycle()
        state = self.s.update_preferences(state, "a", cash_usd="0", broker_return_pct="10", return_date="2026-10-26", now=NOW)
        self.assertTrue(self.s.evaluate(state, "a", history(), now=NOW).actions)
        following = history(["100"] * 199 + ["110", "111"], end="2026-10-27")
        self.assertFalse(self.s.evaluate(state, "a", following, now=datetime(2026, 10, 28, 12, tzinfo=timezone.utc)).actions)
        state["trades"]["opening-TQQQ"]["fee"] = "1"
        self.assertFalse(self.s.evaluate(state, "a", history(), now=NOW).actions)

    def test_entry_confirmation_uses_real_trades_and_remaining_cash(self):
        original = seed()
        decision = self.s.evaluate(original, "a", history(), cash_usd="900", now=NOW)
        original["trades"]["actual"] = trade("actual", "BUY", "2")
        confirmed = self.s.confirm_action(original, "a", decision, decision.actions[0].id, ["actual"], now=NOW)
        config = confirmed["accounts"]["a"]["strategy_200tqqq"]
        self.assertEqual(config["cycle"]["entry_count"], 1)
        self.assertEqual(config["cash_usd"], "680")
        self.assertNotIn("strategy_200tqqq", original["accounts"]["a"])
        same = self.s.evaluate(confirmed, "a", history(), now=NOW)
        self.assertFalse(any(a.kind == "BUY_ENTRY" for a in same.actions))
        next_data = history(["100"] * 199 + ["110", "111"], end="2026-10-27")
        next_day = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
        next_decision = self.s.evaluate(confirmed, "a", next_data, now=next_day)
        next_action = next(a for a in next_decision.actions if a.kind == "BUY_ENTRY")
        self.assertEqual(next_action.budget_usd, Decimal("340"))
        with self.assertRaises(ValueError):
            self.s.confirm_action(confirmed, "a", decision, decision.actions[0].id, ["actual"], now=NOW)

    def test_confirmation_rejects_wrong_account_old_trade_and_duplicate_selection(self):
        state = seed()
        decision = self.s.evaluate(state, "a", history(), cash_usd="900", now=NOW)
        for changes, selected in (({"account_id": "b"}, ["actual"]), ({"executed_at": "2026-10-25"}, ["actual"]), ({}, ["actual", "actual"])):
            candidate = copy.deepcopy(state)
            candidate["trades"]["actual"] = trade("actual", "BUY", "2", **changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.s.confirm_action(candidate, "a", decision, decision.actions[0].id, selected, now=NOW)

    def test_profit_confirmation_requires_actual_quantity_and_blocks_repeat(self):
        state = self.active_cycle()
        decision = self.s.evaluate(state, "a", history(), broker_return_pct="10", now=NOW)
        action = decision.actions[0]
        state["trades"]["sale"] = trade("sale", "SELL", "10")
        confirmed = self.s.confirm_action(state, "a", decision, action.id, ["sale"], now=NOW)
        self.assertEqual(confirmed["accounts"]["a"]["strategy_200tqqq"]["cycle"]["profit_levels"], ["10"])
        self.assertFalse(any(a.kind == "SELL_PROFIT" for a in self.s.evaluate(confirmed, "a", history(), broker_return_pct="10", now=NOW).actions))
        with self.assertRaises(ValueError):
            self.s.confirm_action(confirmed, "a", decision, action.id, ["sale"], now=NOW)

    def test_entry_confirmation_cannot_consume_later_tranche_budget(self):
        state = seed()
        decision = self.s.evaluate(state, "a", history(), cash_usd="900", now=NOW)
        state["trades"]["overbudget"] = trade("overbudget", "BUY", "4")
        with self.assertRaises(ValueError):
            self.s.confirm_action(state, "a", decision, decision.actions[0].id, ["overbudget"], now=NOW)

    def test_future_timestamp_and_preclose_fill_are_rejected(self):
        for stamp in ("2026-10-27T23:00:00Z", "2026-10-26T19:00:00Z"):
            state = seed()
            decision = self.s.evaluate(state, "a", history(), cash_usd="900", now=NOW)
            state["trades"]["bad-time"] = trade("bad-time", "BUY", "2", executed_at=stamp)
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                self.s.confirm_action(state, "a", decision, decision.actions[0].id, ["bad-time"], now=NOW)

    def test_two_exit_confirmations_wait_for_both_recorded_legs(self):
        state = self.active_cycle(spym="5")
        decision = self.s.evaluate(state, "a", history(["100"] * 200 + ["90"]), now=NOW)
        state["trades"]["exit-main"] = trade("exit-main", "SELL", "100")
        state["trades"]["exit-growth"] = trade("exit-growth", "SELL", "5", symbol="SPYM")
        first = self.s.confirm_action(state, "a", decision, decision.actions[0].id, ["exit-main"], now=NOW)
        self.assertIsNotNone(first["accounts"]["a"]["strategy_200tqqq"]["cycle"])
        second = self.s.confirm_action(first, "a", decision, decision.actions[1].id, ["exit-growth"], now=NOW)
        self.assertIsNone(second["accounts"]["a"]["strategy_200tqqq"]["cycle"])
        self.assertEqual(len(second["accounts"]["a"]["strategy_200tqqq"]["confirmations"]), 2)

    def test_spym_partial_fill_plan_has_fresh_identity_for_remaining_proceeds(self):
        state = self.active_cycle()
        decision = self.s.evaluate(state, "a", history(), broker_return_pct="10", now=NOW)
        state["trades"]["sale"] = trade("sale", "SELL", "10")
        state = self.s.confirm_action(state, "a", decision, decision.actions[0].id, ["sale"], now=NOW)
        pending = self.s.evaluate(state, "a", history(), now=NOW)
        buy = next(item for item in pending.actions if item.kind == "BUY_SPYM")
        state["trades"]["buy-spym"] = trade("buy-spym", "BUY", "5", "100", symbol="SPYM")
        state = self.s.confirm_action(state, "a", pending, buy.id, ["buy-spym"], now=NOW)
        next_plan = self.s.evaluate(state, "a", history(), now=NOW)
        remaining = next(item for item in next_plan.actions if item.kind == "BUY_SPYM")
        self.assertEqual(remaining.budget_usd, Decimal("600"))
        self.assertNotEqual(remaining.id, buy.id)

    def test_initial_intent_ids_are_account_scoped_and_repeatable(self):
        state = seed()
        first = self.s.evaluate(state, "a", history(), cash_usd="900", now=NOW)
        repeated = self.s.evaluate(state, "a", history(), cash_usd="900", now=NOW)
        other = self.s.evaluate(state, "b", history(), cash_usd="900", now=NOW)
        self.assertEqual(first.actions[0].id, repeated.actions[0].id)
        self.assertNotEqual(first.actions[0].id, other.actions[0].id)

    def test_simultaneous_entry_and_profit_requires_manual_review(self):
        state = self.s.adopt_cycle(seed("100"), "a", start_date="2026-09-01", last_entry_date="2026-10-23", entry_count=1, now=NOW)
        decision = self.s.evaluate(state, "a", history(), cash_usd="900", broker_return_pct="10", now=NOW)
        self.assertEqual({item.kind for item in decision.actions}, {"BUY_ENTRY", "SELL_PROFIT"})
        self.assertTrue(decision.manual_review)

    def test_cycle_correction_preserves_uninvested_profit_proceeds(self):
        state = self.active_cycle()
        state["accounts"]["a"]["strategy_200tqqq"]["cycle"]["pending_spym_usd"] = "1100"
        corrected = self.s.adopt_cycle(state, "a", start_date="2026-09-01", entry_count=3, profit_levels=("10",), now=NOW)
        self.assertEqual(corrected["accounts"]["a"]["strategy_200tqqq"]["cycle"]["pending_spym_usd"], "1100")

    def test_history_acknowledgement_cannot_override_stale_exchange_session(self):
        stale = history(end="2026-10-23", adjustment="unknown")
        with self.assertRaises(ValueError):
            self.s.confirm_history(seed(), "a", stale, now=NOW)

    def test_early_close_execution_can_confirm_after_actual_exchange_close(self):
        now = datetime(2026, 11, 28, 12, tzinfo=timezone.utc)
        state = self.active_cycle()
        decision = self.s.evaluate(state, "a", history(["100"] * 200 + ["90"], end="2026-11-27"), now=now)
        state["trades"]["early-exit"] = trade("early-exit", "SELL", "100", executed_at="2026-11-27T18:30:00Z")
        confirmed = self.s.confirm_action(state, "a", decision, decision.actions[0].id, ["early-exit"], now=now)
        self.assertIsNone(confirmed["accounts"]["a"]["strategy_200tqqq"]["cycle"])

    def test_unknown_exchange_calendar_year_is_blocked(self):
        decision = self.s.evaluate(seed(), "a", history(end="2029-10-26"), cash_usd="900", now=datetime(2029, 10, 27, 12, tzinfo=timezone.utc))
        self.assertEqual(decision.status, "blocked")
        self.assertFalse(decision.actions)

    def test_decision_snapshot_roundtrips_exact_decimals_and_actual_confirmation(self):
        state = seed()
        decision = self.s.evaluate(state, "a", history(), cash_usd="1000", now=NOW)
        self.assertTrue(callable(getattr(self.s, "decision_to_dict", None)), "Decision serializer is missing")
        self.assertTrue(callable(getattr(self.s, "decision_from_dict", None)), "Decision deserializer is missing")
        payload = self.s.decision_to_dict(decision)
        self.assertEqual(payload["close"], "110")
        self.assertEqual(payload["positions"]["TQQQ"]["quantity"], "0")
        self.assertTrue(payload["actions"][0]["budget_usd"].startswith("333.333333333333333333333333333333"))
        restored = self.s.decision_from_dict(json.loads(json.dumps(payload, allow_nan=False)))
        self.assertEqual(restored, decision)
        state["trades"]["actual"] = trade("actual", "BUY", "3")
        confirmed = self.s.confirm_action(state, "a", restored, restored.actions[0].id, ["actual"], now=NOW)
        self.assertEqual(confirmed["accounts"]["a"]["strategy_200tqqq"]["cash_usd"], "670")
        payload["close"] = 110.5
        with self.assertRaises(ValueError):
            self.s.decision_from_dict(payload)

    def test_unverified_exit_net_cash_blocks_entry_until_user_updates_budget(self):
        state = seed()
        state["accounts"]["a"]["strategy_200tqqq"] = {"version": 1, "cash_usd": "900", "reserve_review_required": True,
                                                       "reserve_review_note": "실제 매도 수수료·세금 미확인"}
        blocked = self.s.evaluate(state, "a", history(), now=NOW)
        self.assertEqual(blocked.status, "blocked")
        self.assertFalse(blocked.actions)
        ordinary = self.s.update_preferences(state, "a", cash_usd="1200", now=NOW)
        self.assertTrue(ordinary["accounts"]["a"]["strategy_200tqqq"].get("reserve_review_required"))
        reviewed = self.s.update_preferences(state, "a", cash_usd="1200", costs_reviewed=True, now=NOW)
        self.assertFalse(reviewed["accounts"]["a"]["strategy_200tqqq"].get("reserve_review_required"))
        decision = self.s.evaluate(reviewed, "a", history(), now=NOW)
        self.assertEqual(decision.actions[0].budget_usd, Decimal("400"))

    def test_uncertain_trade_costs_block_new_risk_but_allow_full_exit(self):
        state = self.active_cycle(spym="5")
        config = state["accounts"]["a"]["strategy_200tqqq"]
        config.update(costs_review_required=True, costs_review_note="실제 매도 비용 확인 필요")
        config["cycle"]["pending_spym_usd"] = "1100"
        blocked = self.s.evaluate(state, "a", history(), broker_return_pct="10", now=NOW)
        self.assertEqual(blocked.status, "blocked")
        self.assertFalse(blocked.actions)
        exit_plan = self.s.evaluate(state, "a", history(["100"] * 200 + ["90"]), now=NOW)
        self.assertEqual({action.symbol for action in exit_plan.actions}, {"TQQQ", "SPYM"})
        ordinary = self.s.update_preferences(state, "a", cash_usd="0", pending_spym_usd="1098.75", now=NOW)
        self.assertTrue(ordinary["accounts"]["a"]["strategy_200tqqq"]["costs_review_required"])
        reviewed = self.s.update_preferences(state, "a", cash_usd="0", pending_spym_usd="1098.75", costs_reviewed=True, now=NOW)
        reviewed_config = reviewed["accounts"]["a"]["strategy_200tqqq"]
        self.assertFalse(reviewed_config.get("costs_review_required"))
        self.assertEqual(reviewed_config["cycle"]["pending_spym_usd"], "1098.75")
        with self.assertRaises(ValueError):
            self.s.update_preferences(state, "a", cash_usd="0", costs_reviewed=True, now=NOW)

    def test_verified_nh_execution_window_accepts_next_korean_business_date(self):
        now = datetime(2026, 10, 26, 22, tzinfo=timezone.utc)
        state = seed()
        decision = self.s.evaluate(state, "a", history(), cash_usd="900", now=now)
        uid = "nh-us:synthetic:2026-10-27:123"
        state["trades"][uid] = trade(uid, "BUY", "2", executed_at="2026-10-27", source="NH", external_id=uid)
        with self.assertRaises(ValueError):
            self.s.confirm_action(state, "a", decision, decision.actions[0].id, [uid], now=now)
        evidence = {uid: {"submitted_at": "2026-10-26T21:00:00Z", "observed_at": "2026-10-26T21:30:00Z"}}
        confirmed = self.s.confirm_action(state, "a", decision, decision.actions[0].id, [uid], execution_evidence=evidence, now=now)
        self.assertEqual(confirmed["accounts"]["a"]["strategy_200tqqq"]["cycle"]["entry_count"], 1)
        self.assertEqual(confirmed["trades"][uid]["executed_at"], "2026-10-27", "Broker date must not become an invented fill timestamp")
        for changes in ({"submitted_at": "2026-10-26T19:00:00Z"}, {"observed_at": "2026-10-26T23:00:00Z"},
                        {"observed_at": "2026-10-26T20:30:00Z"}, {"submitted_at": "2026-10-26T21:00:00"}):
            invalid = {uid: {**evidence[uid], **changes}}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.s.confirm_action(state, "a", decision, decision.actions[0].id, [uid], execution_evidence=invalid, now=now)
        for changes in ({"source": "manual"}, {"executed_at": "2026-10-28"}, {"external_id": "unverified"}):
            invalid_state = copy.deepcopy(state)
            invalid_state["trades"][uid].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.s.confirm_action(invalid_state, "a", decision, decision.actions[0].id, [uid], execution_evidence=evidence, now=now)

    def test_reset_is_blocked_until_tqqq_and_spym_are_flat(self):
        state = self.active_cycle(spym="5")
        with self.assertRaises(ValueError):
            self.s.reset_cycle(state, "a", now=NOW)
        state["trades"]["exit1"] = trade("exit1", "SELL", "100")
        with self.assertRaises(ValueError):
            self.s.reset_cycle(state, "a", now=NOW)
        state["trades"]["exit2"] = trade("exit2", "SELL", "5", symbol="SPYM")
        reset = self.s.reset_cycle(state, "a", now=NOW)
        self.assertIsNone(reset["accounts"]["a"]["strategy_200tqqq"]["cycle"])

    def test_account_selection_and_cash_inputs_are_validated(self):
        state = seed()
        for account, cash in ((None, "100"), ("missing", "100"), ("a", "-1"), ("a", 1.5)):
            with self.subTest(account=account, cash=cash), self.assertRaises(ValueError):
                self.s.evaluate(state, account, history(), cash_usd=cash, now=NOW)

    def test_close_csv_is_read_without_inventing_adjustment_or_fills(self):
        try:
            ui = importlib.import_module("stockjournal.strategy_ui")
        except ModuleNotFoundError:
            self.fail("The strategy CSV reader is not implemented")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily.csv"
            path.write_text("date,close\n2026-10-23,100.1234\n2026-10-26,110\n", encoding="utf-8-sig")
            data = ui.read_close_csv(path)
            self.assertEqual(data["rows"], [{"date": "2026-10-23", "close": "100.1234"}, {"date": "2026-10-26", "close": "110"}])
            self.assertEqual(data["adjustment"], "unknown")
            self.assertEqual(data["finalization"], "user_verified")
            path.write_text("date,volume\n2026-10-26,1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                ui.read_close_csv(path)


if __name__ == "__main__":
    unittest.main()
