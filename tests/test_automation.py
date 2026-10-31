"""Execution safeguards use a deterministic broker, never a live account."""
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import queue

from stockjournal.automation import (
    AutomationBlocked, AutomationEngine, BrokerSnapshot, ExecutionJournal,
    ExecutionPolicy, OrderUpdate, OwnershipError, Quote, SignalEvidence,
)
from stockjournal.strategy import Action, Decision


NOW = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)


class FakeBroker:
    """Only the broker boundary is replaced; journals and engine are real."""
    account_id = "nh-fingerprint"

    def __init__(self):
        self.now = NOW
        self.cash = D("1000")
        self.holdings = {"TQQQ": D("100"), "SPYM": D("10"), "SGOV": D("0")}
        self.requests = []
        self.cancelled = []
        self.updates = {}
        self.next_status = "SUBMITTED"
        self.raise_submit = False
        self.pending = ()
        self.snapshot_age = 0
        self.quote_age = 0
        self.verified = True
        self.quote_prices = {"TQQQ": D("50"), "SPYM": D("70"), "SGOV": D("100")}
        self.on_submit = None

    def snapshot(self, account_id, symbols):
        return BrokerSnapshot(self.account_id, self.now - timedelta(seconds=self.snapshot_age),
            copy.deepcopy(self.holdings), self.cash, self.pending, True,
            NOW + timedelta(hours=5), self.verified)

    def quote(self, account_id, symbol):
        return Quote(symbol, self.quote_prices[symbol], self.now - timedelta(seconds=self.quote_age))

    def submit_limit(self, request):
        self.requests.append(request)
        if self.on_submit:
            self.on_submit(request)
        if self.raise_submit:
            raise TimeoutError("synthetic network timeout")
        order_id = "order-" + str(len(self.requests))
        update = OrderUpdate(order_id, self.next_status, updated_at=self.now)
        self.updates[order_id] = update
        return update

    def order_status(self, account_id, broker_order_id):
        return self.updates[broker_order_id]

    def cancel(self, account_id, broker_order_id):
        self.cancelled.append(broker_order_id)
        update = replace(self.updates[broker_order_id], status="CANCELLED", cancel_requested=True,
                         updated_at=self.now)
        self.updates[broker_order_id] = update
        return update

    def fill(self, order_id, quantity=None):
        request = self.requests[int(order_id.split("-")[-1]) - 1]
        qty = D(request.quantity) if quantity is None else D(quantity)
        difference = qty - self.updates[order_id].filled_quantity
        sign = 1 if request.side == "BUY" else -1
        self.holdings[request.symbol] += difference * sign
        self.cash -= difference * request.limit_price * sign
        self.updates[order_id] = OrderUpdate(order_id,
            "FILLED" if qty == request.quantity else "PARTIAL", qty,
            request.limit_price, self.now, commission_usd=D("0"), tax_usd=D("0"))


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "execution-local.sqlite3"
        self.journal = ExecutionJournal(self.path, "machine-a")
        self.broker = FakeBroker()
        self.engine = AutomationEngine(self.journal, self.broker, now=lambda: self.broker.now)
        self.decision = Decision(account_id="local-account", status="profit", as_of="2026-10-29",
            history_hash="history-one", evaluated_at=NOW.isoformat(),
            positions={s: {"quantity": q} for s, q in self.broker.holdings.items()},
            actions=[Action("profit-10", "SELL_PROFIT", "TQQQ", D("10"), None, "profit", D("10"))])
        self.evidence = SignalEvidence("local-account", "history-one", "2026-10-29", "verified-csv",
            "2026-10-29", True, True, True, NOW, broker_return_verified=True)
        self.policy = ExecutionPolicy("local-account", self.broker.account_id, D("10000"),
            D("1000"), mode="LIVE", deadline_at=NOW + timedelta(minutes=10))

    def arm(self):
        self.engine.arm(self.policy)

    def start(self, *, dry=False):
        if not dry:
            self.arm()
        return self.engine.start(self.decision, replace(self.policy, mode="DRY_RUN") if dry else self.policy,
                                 self.evidence)

    def test_dry_run_never_submits_or_changes_broker_holdings(self):
        before = copy.deepcopy(self.broker.holdings)
        workflow = self.start(dry=True)
        for _ in range(5):
            self.engine.tick(workflow["id"])
        result = self.journal.get(workflow["id"])
        self.assertEqual(result["status"], "SIMULATED")
        self.assertEqual([order["status"] for order in result["orders"]], ["SIMULATED", "SIMULATED"])
        self.assertEqual(self.broker.requests, [])
        self.assertEqual(self.broker.holdings, before)

    def test_intent_is_committed_before_submit_and_duplicate_start_does_not_resend(self):
        workflow = self.start()
        self.broker.on_submit = lambda request: self.assertEqual(
            self.journal.get(workflow["id"])["orders"][0]["status"], "INTENT")
        self.engine.tick(workflow["id"])
        again = self.engine.start(self.decision, self.policy, self.evidence)
        self.engine.tick(again["id"])
        self.assertEqual(workflow["id"], again["id"])
        self.assertEqual(len(self.broker.requests), 1)

    def test_dependent_buy_waits_for_full_fill_and_verified_available_cash(self):
        workflow = self.start()
        self.engine.tick(workflow["id"])
        self.engine.tick(workflow["id"])
        self.assertEqual(len(self.broker.requests), 1)
        self.broker.fill("order-1")
        self.engine.tick(workflow["id"])
        self.engine.tick(workflow["id"])
        self.assertEqual([(r.side, r.symbol) for r in self.broker.requests], [("SELL", "TQQQ"), ("BUY", "SPYM")])
        self.assertLessEqual(self.broker.requests[1].quantity * self.broker.requests[1].limit_price, D("499.50"))

    def test_sold_proceeds_are_not_spendable_until_broker_cash_increases(self):
        workflow = self.start()
        self.engine.tick(workflow["id"])
        self.broker.fill("order-1")
        self.broker.cash = D("1000")
        self.engine.tick(workflow["id"])
        self.engine.tick(workflow["id"])
        self.assertEqual(len(self.broker.requests), 1)
        self.assertEqual(self.journal.get(workflow["id"])["status"], "WAIT_CASH")

    def test_partial_fill_blocks_every_following_leg(self):
        workflow = self.start()
        self.engine.tick(workflow["id"])
        self.broker.fill("order-1", "4")
        for _ in range(3):
            self.engine.tick(workflow["id"])
        self.assertEqual(self.journal.get(workflow["id"])["status"], "BLOCKED")
        self.assertEqual(len(self.broker.requests), 1)

    def test_uncertain_submit_is_never_retried_even_after_restart(self):
        self.broker.raise_submit = True
        workflow = self.start()
        self.engine.tick(workflow["id"])
        second = AutomationEngine(ExecutionJournal(self.path, "machine-a"), self.broker, now=lambda: NOW)
        second.arm(self.policy)
        for _ in range(3):
            second.tick(workflow["id"])
            second.reconcile(workflow["id"])
        self.assertEqual(len(self.broker.requests), 1)
        self.assertEqual(self.journal.get(workflow["id"])["orders"][0]["status"], "UNKNOWN")

    def test_restart_requires_reconciliation_and_does_not_resume_dependent_buy(self):
        workflow = self.start()
        self.engine.tick(workflow["id"])
        self.broker.fill("order-1")
        second = AutomationEngine(ExecutionJournal(self.path, "machine-a"), self.broker, now=lambda: NOW)
        second.arm(self.policy)
        second.tick(workflow["id"])
        second.reconcile(workflow["id"])
        second.tick(workflow["id"])
        self.assertEqual(len(self.broker.requests), 1)
        self.assertEqual(self.journal.get(workflow["id"])["status"], "REVIEW_REQUIRED")

    def test_deadline_cancels_once_and_never_sends_target_buy(self):
        workflow = self.start()
        self.engine.tick(workflow["id"])
        self.broker.now += timedelta(minutes=11)
        for _ in range(3):
            self.engine.tick(workflow["id"])
        self.assertEqual(self.broker.cancelled, ["order-1"])
        self.assertEqual(len(self.broker.requests), 1)
        self.assertEqual(self.journal.get(workflow["id"])["status"], "BLOCKED")

    def test_mismatched_account_stale_data_and_unconfirmed_signal_block(self):
        for change in ({"account_id": "another"}, {"history_hash": "changed"},
                       {"adjustment_confirmed": False}, {"latest_session_verified": False},
                       {"broker_return_verified": False}):
            with self.subTest(change=change), self.assertRaises(AutomationBlocked):
                self.arm()
                self.engine.start(self.decision, self.policy, replace(self.evidence, **change))
        self.assertEqual(self.broker.requests, [])

    def test_fresh_broker_holdings_must_equal_journal_before_first_order(self):
        workflow = self.start()
        self.broker.holdings["TQQQ"] = D("99")
        self.engine.tick(workflow["id"])
        self.assertEqual(self.journal.get(workflow["id"])["status"], "BLOCKED")
        self.assertEqual(self.broker.requests, [])

    def test_stale_quote_or_pending_order_prevents_submission(self):
        for field, value in (("quote_age", 120), ("snapshot_age", 120), ("pending", ("external",)),
                             ("verified", False)):
            with self.subTest(field=field):
                path = Path(self.directory.name) / (field + ".sqlite3")
                broker = FakeBroker()
                setattr(broker, field, value)
                engine = AutomationEngine(ExecutionJournal(path, "machine-a"), broker, now=lambda: NOW)
                engine.arm(self.policy)
                workflow = engine.start(self.decision, self.policy, self.evidence)
                engine.tick(workflow["id"])
                self.assertEqual(engine.journal.get(workflow["id"])["status"], "BLOCKED")
                self.assertEqual(broker.requests, [])

    def test_budget_cap_and_fee_reserve_bound_a_repriced_integer_buy(self):
        self.decision.actions = [Action("entry-1", "BUY_ENTRY", "TQQQ", D("2"), D("120"), "entry", entry_number=1)]
        self.policy = replace(self.policy, max_order_usd=D("120"), max_buy_usd=D("120"))
        workflow = self.start()
        self.engine.tick(workflow["id"])
        request = self.broker.requests[0]
        self.assertEqual(request.quantity, 2)
        self.assertEqual(request.limit_price, D("50.05"))
        self.assertLessEqual(request.quantity * request.limit_price * D("1.005"), D("120"))

    def test_entry_budget_larger_than_cap_is_blocked_without_advancing_tranche(self):
        self.decision.actions = [Action("entry-1", "BUY_ENTRY", "TQQQ", D("20"), D("1000"), "entry", entry_number=1)]
        self.policy = replace(self.policy, max_order_usd=D("120"), max_buy_usd=D("120"))
        workflow = self.start()
        self.engine.tick(workflow["id"])
        self.assertEqual(self.journal.get(workflow["id"])["status"], "BLOCKED")
        self.assertEqual(self.broker.requests, [])

    def test_sgov_funding_sells_only_needed_shares_then_waits_for_fill(self):
        self.broker.cash = D("0")
        self.broker.holdings["SGOV"] = D("30")
        self.decision.positions["SGOV"]["quantity"] = D("30")
        self.decision.actions = [Action("entry-1", "BUY_ENTRY", "TQQQ", D("20"), D("1000"), "entry", entry_number=1)]
        self.evidence = replace(self.evidence, entry_funding_from_sgov=True)
        workflow = self.start()
        self.engine.tick(workflow["id"])
        self.assertEqual((self.broker.requests[0].symbol, self.broker.requests[0].side), ("SGOV", "SELL"))
        self.assertEqual(self.broker.requests[0].quantity, 11)
        self.engine.tick(workflow["id"])
        self.assertEqual(len(self.broker.requests), 1)
        self.broker.fill("order-1")
        self.engine.tick(workflow["id"])
        self.engine.tick(workflow["id"])
        self.assertEqual(self.broker.requests[1].symbol, "TQQQ")

    def test_copied_execution_database_cannot_be_owned_by_another_machine(self):
        with self.assertRaises(OwnershipError):
            ExecutionJournal(self.path, "machine-b")

    def test_live_mode_is_unarmed_by_default_and_stop_prevents_new_legs(self):
        with self.assertRaises(AutomationBlocked):
            self.engine.start(self.decision, self.policy, self.evidence)
        workflow = self.start()
        self.engine.tick(workflow["id"])
        self.broker.fill("order-1")
        self.engine.stop()
        self.engine.tick(workflow["id"])
        self.assertEqual(len(self.broker.requests), 1)

    def test_filled_response_inconsistent_with_limit_is_unknown(self):
        workflow = self.start()
        self.engine.tick(workflow["id"])
        self.broker.updates["order-1"] = OrderUpdate("order-1", "FILLED", D("10"), D("1"), NOW)
        self.engine.tick(workflow["id"])
        result = self.journal.get(workflow["id"])
        self.assertEqual(result["orders"][0]["status"], "UNKNOWN")
        self.assertEqual(result["status"], "BLOCKED")

    def test_second_workflow_waits_for_actual_trade_import_acknowledgement(self):
        workflow = self.start()
        self.engine.tick(workflow["id"])
        self.broker.fill("order-1")
        self.engine.tick(workflow["id"])
        self.engine.tick(workflow["id"])
        self.broker.fill("order-2")
        self.engine.tick(workflow["id"])
        self.engine.tick(workflow["id"])
        self.assertEqual(self.journal.get(workflow["id"])["status"], "COMPLETE")
        next_decision = copy.deepcopy(self.decision)
        next_decision.actions = [Action("profit-25", "SELL_PROFIT", "TQQQ", D("9"), None, "profit", D("25"))]
        with self.assertRaises(AutomationBlocked):
            self.engine.start(next_decision, self.policy, self.evidence)
        self.engine.mark_applied(workflow["id"])
        self.assertEqual(self.engine.start(next_decision, self.policy, self.evidence)["status"], "RUNNING")

    def test_pre_submit_crash_intent_is_not_resubmitted(self):
        workflow = self.start()
        def crash(request):
            raise KeyboardInterrupt("process stopped after durable intent")
        self.broker.on_submit = crash
        with self.assertRaises(KeyboardInterrupt):
            self.engine.tick(workflow["id"])
        restarted = AutomationEngine(ExecutionJournal(self.path, "machine-a"), self.broker, now=lambda: NOW)
        restarted.arm(self.policy)
        restarted.tick(workflow["id"])
        self.assertEqual(self.journal.get(workflow["id"])["orders"][0]["status"], "UNKNOWN")
        self.assertEqual(len(self.broker.requests), 1)

    def test_exit_sells_both_assets_before_sgov_buy(self):
        self.policy = replace(self.policy, max_buy_usd=D("10000"))
        self.decision.status = "exit"
        self.decision.actions = [Action("exit-tqqq", "EXIT", "TQQQ", D("100"), None, "exit"),
                                 Action("exit-spym", "EXIT", "SPYM", D("10"), None, "exit")]
        workflow = self.start()
        self.engine.tick(workflow["id"])
        self.broker.fill("order-1")
        self.engine.tick(workflow["id"])
        self.engine.tick(workflow["id"])
        self.assertEqual([r.symbol for r in self.broker.requests], ["TQQQ", "SPYM"])
        self.broker.fill("order-2")
        self.engine.tick(workflow["id"])
        self.engine.tick(workflow["id"])
        self.assertEqual([r.symbol for r in self.broker.requests], ["TQQQ", "SPYM", "SGOV"])
        self.assertEqual(self.broker.requests[2].quantity, 56)

    def test_exit_parks_allocated_cash_without_spending_unallocated_cash(self):
        self.policy = replace(self.policy, max_buy_usd=D("10000"))
        self.decision.status = "exit"
        self.decision.actions = [Action("exit-tqqq", "EXIT", "TQQQ", D("100"), None, "exit"),
                                 Action("exit-spym", "EXIT", "SPYM", D("10"), None, "exit")]
        self.evidence = replace(self.evidence, exit_reserve_cash_usd=D("500"))
        workflow = self.start()
        self.engine.tick(workflow["id"])
        self.broker.fill("order-1")
        self.engine.tick(workflow["id"])
        self.engine.tick(workflow["id"])
        self.broker.fill("order-2")
        self.engine.tick(workflow["id"])
        self.engine.tick(workflow["id"])
        self.assertEqual(self.broker.requests[2].symbol, "SGOV")
        self.assertEqual(self.broker.requests[2].quantity, 61)
        self.assertLessEqual(self.broker.requests[2].quantity * self.broker.requests[2].limit_price * D("1.005"), D("6194.30"))

    def test_exit_cash_reserve_larger_than_initial_available_cash_blocks_first_sell(self):
        self.decision.status = "exit"
        self.decision.actions = [Action("exit-tqqq", "EXIT", "TQQQ", D("100"), None, "exit")]
        self.evidence = replace(self.evidence, exit_reserve_cash_usd=D("1001"))
        workflow = self.start()
        self.engine.tick(workflow["id"])
        self.assertEqual(self.journal.get(workflow["id"])["status"], "BLOCKED")
        self.assertEqual(self.broker.requests, [])

    def test_cancel_timeout_is_not_retried(self):
        workflow = self.start()
        self.engine.tick(workflow["id"])
        def unknown_cancel(account_id, order_id):
            self.broker.cancelled.append(order_id)
            raise TimeoutError("cancel result unknown")
        self.broker.cancel = unknown_cancel
        self.broker.now += timedelta(minutes=11)
        for _ in range(3):
            self.engine.tick(workflow["id"])
        self.assertEqual(self.broker.cancelled, ["order-1"])
        self.assertEqual(self.journal.get(workflow["id"])["orders"][0]["status"], "UNKNOWN")

    def test_dry_run_new_budget_can_be_previewed_without_consuming_live_action(self):
        first = self.start(dry=True)
        second_policy = replace(self.policy, mode="DRY_RUN", max_buy_usd=D("500"))
        second = self.engine.start(self.decision, second_policy, self.evidence)
        self.assertNotEqual(first["id"], second["id"])
        live = self.start()
        self.engine.tick(live["id"])
        self.assertEqual(len(self.broker.requests), 1)

    def test_stop_event_reaches_adapter_before_its_late_network_write(self):
        broker = self.broker
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        broker.stop_event = None
        broker.set_stop_event = lambda event: setattr(broker, "stop_event", event)
        original_submit = broker.submit_limit
        def preflight_then_write(request):
            entered.set()
            release.wait(2)
            if broker.stop_event is not None and broker.stop_event.is_set():
                raise AutomationBlocked("cancelled before network write")
            return original_submit(request)
        broker.submit_limit = preflight_then_write
        self.engine.broker = broker
        workflow = self.start()
        worker = threading.Thread(target=lambda: self.engine.tick(workflow["id"]))
        worker.start()
        self.assertTrue(entered.wait(2))
        self.engine.stop()
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(broker.requests, [])

    def test_ui_stop_during_engine_startup_cannot_rearm_worker(self):
        from stockjournal.automation import AutomationContext
        from stockjournal.automation_ui import AutomationPanel
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        class DelayedEngine(AutomationEngine):
            def __init__(self, *args):
                super().__init__(*args, now=lambda: NOW)
                entered.set()
                release.wait(2)
        panel = AutomationPanel.__new__(AutomationPanel)
        panel._closed = False
        panel.busy = False
        panel.engine = None
        panel._after_id = None
        panel.workflow_id = None
        panel._cancel = threading.Event()
        panel._control_lock = threading.Lock()
        panel._results = queue.Queue()
        panel.journal = self.journal
        panel._options = {"account_id": "local-account", "mode": "LIVE", "maximum": D("10000"), "total": D("1000"), "interval": 60}
        panel._set_running_controls = lambda: None
        panel.status = SimpleNamespace(set=lambda value: None)
        panel.app = SimpleNamespace(account_id=lambda: "local-account", busy=False,
            prepare_automation_context=lambda: lambda: AutomationContext(self.decision, self.evidence, self.broker))
        with patch("stockjournal.automation_ui.AutomationEngine", DelayedEngine):
            panel._dispatch("start", arm=True)
            self.assertTrue(entered.wait(2))
            panel.stop()
            release.set()
            kind, result = panel._results.get(timeout=2)
        self.assertEqual(self.broker.requests, [])
        self.assertEqual(kind, "stopped")


if __name__ == "__main__":
    unittest.main()
