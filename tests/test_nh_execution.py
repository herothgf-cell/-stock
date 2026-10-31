import importlib
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from stockjournal.providers import NHProvider, ProviderError
from tests.test_providers import FixtureHTTP, Response, token


NOW = datetime(2026, 10, 1, 21, 0, tzinfo=timezone.utc)
ACCOUNT = {"account_number": "12345678901", "account_type": "01", "environment": "live"}


def current(period="2"):
    return Response({"Output_0": {"iem_cd": "TQQQ", "trdprc": "49.12", "currency_unit": "USD",
                                  "trade_date": "20261001", "marketperiod_cls": period}})


def availability(**changes):
    return Response({"Output_0": {"wtm_cur_cd": "USD", "fc_dca": "1000", "orr_pbl_amt": "1000",
        "orr_pbl_qty": 20, "byn_pbl_qty": 20, "max_pbl_amt": "99999", "max_pbl_qty": 9999,
        "sll_pbl_qty": 10, "hld_qty": 10, "csh_wtm": "0", "ect_use_amt": "0", **changes}})


def order_row(**changes):
    return {"orr_no": 123, "org_orr_no": 0, "orr_dt": "20261001", "iem_cd": "TQQQ",
        "oss_sby_dit_cd": "2", "fc_sec_trd_nat_cd": "200", "orr_qty": 5, "fc_orr_uit_pr": "49.12",
        "cns_qty": 0, "cns_pr": "0", "ny_cns_orr_qty": 5, "can_qty": 0, "cor_qty": 0,
        "oss_ato_orr_sts_cd": "1", "cfd_lon_cd": "00", **changes}


def orders(*rows, headers=None):
    return Response({"Output_0": list(rows), "rsp_msg": "조회가 완료되었습니다." if rows else "조회내역이 없습니다."}, headers=headers)


class NHExecutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        try:
            self.nh = importlib.import_module("stockjournal.nh_execution")
            self.auto = importlib.import_module("stockjournal.automation")
        except ImportError as error:
            self.fail(f"Execution adapter/protocol is not implemented: {error}")
        self.clock = patch.object(self.nh, "_now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        sleeping = patch.object(self.nh.time, "sleep")
        sleeping.start()
        self.addCleanup(sleeping.stop)

    def adapter(self, http, **options):
        with patch("stockjournal.providers.build_opener", return_value=http):
            provider = NHProvider("fixture-key", "fixture-secret", Path(self.directory.name))
        settings = dict(environment="live", session="afterhours", enabled=True,
                        session_verified=True, cash_account_verified=True)
        settings.update(options)
        return self.nh.NHExecutionAdapter(provider, settings.pop("account", ACCOUNT), **settings)

    def request(self, adapter, **changes):
        fields = dict(account_id=adapter.account_id, symbol="TQQQ", side="BUY", quantity=5,
                      limit_price=Decimal("49.12"), intent_id="intent-one")
        fields.update(changes)
        return self.auto.OrderRequest(**fields)

    def submit_fixture(self, *later):
        http = FixtureHTTP(token(), current(), availability(), Response({"Output_0": {"orr_no": 123}}), *later)
        adapter = self.adapter(http)
        return http, adapter, adapter.submit_limit(self.request(adapter))

    def test_default_disabled_or_unverified_settings_make_no_http_request(self):
        for changes in ({"enabled": False}, {"session_verified": False}, {"cash_account_verified": False}):
            http = FixtureHTTP()
            adapter = self.adapter(http, **changes)
            with self.subTest(changes=changes), self.assertRaises(ProviderError):
                adapter.submit_limit(self.request(adapter))
            self.assertEqual(http.requests, [])

    def test_environment_requires_matching_discovered_account_type(self):
        for settings in ({"environment": "mock"}, {"environment": "https://evil.invalid"},
                         {"account": {**ACCOUNT, "account_type": "99"}}):
            with self.subTest(settings=settings), self.assertRaises(ProviderError):
                self.adapter(FixtureHTTP(), **settings)
        http = FixtureHTTP(token(), current())
        adapter = self.adapter(http, environment="mock", account={**ACCOUNT, "account_type": "03", "environment": "mock"})
        self.assertEqual(adapter.quote(adapter.account_id, "TQQQ").price, Decimal("49.12"))
        self.assertEqual(http.requests[1].full_url, "https://moapi.nhplug.com:8443/gbstock/quote/v1/current")

    def test_exact_decimal_afterhours_submission_uses_cash_limit_contract_once(self):
        http, adapter, result = self.submit_fixture()
        self.assertEqual(result.status, "SUBMITTED")
        request = http.requests[-1]
        self.assertEqual(request.full_url, "https://api.nhplug.com:8443/gbstock/order/v1/buy")
        self.assertEqual(json.loads(request.data, parse_float=Decimal), {"Input_0": {"act_no": "12345678901",
            "fc_sec_trd_nat_cd": "200", "iem_cd": "TQQQ", "orr_qty": 5, "fc_orr_uit_pr": Decimal("49.12"),
            "ahi_nmn_pr_tp_cd": "62", "wtm_cur_knd_cd": "1"}})
        self.assertIn(b'"fc_orr_uit_pr":49.12', request.data)
        self.assertNotIn("x-client-secret", {key.lower(): value for key, value in request.header_items()})
        before = len(http.requests)
        self.assertEqual(adapter.submit_limit(self.request(adapter)), result)
        self.assertEqual(len(http.requests), before)
        handle = self.nh.decode_order_handle(result.broker_order_id)
        self.assertEqual((handle["order_number"], handle["symbol"], handle["quantity"]), ("123", "TQQQ", 5))
        self.assertEqual(handle["candidate_dates"], ["2026-10-01", "2026-10-02"])
        self.assertNotIn("12345678901", result.broker_order_id)

    def test_same_intent_with_different_quantity_never_reuses_success(self):
        http, adapter, _ = self.submit_fixture()
        with self.assertRaises(ProviderError):
            adapter.submit_limit(self.request(adapter, quantity=4))
        self.assertEqual(len(http.requests), 4)

    def test_insufficient_cash_never_uses_margin_maximum_or_writes_order(self):
        for fields in ({"fc_dca": "1"}, {"orr_pbl_amt": "1"}, {"wtm_cur_cd": "KRW"},
                       {"csh_wtm": "900"}, {"orr_pbl_qty": 4}):
            http = FixtureHTTP(token(), current(), availability(**fields))
            adapter = self.adapter(http)
            with self.subTest(fields=fields), self.assertRaises(ProviderError):
                adapter.submit_limit(self.request(adapter))
            self.assertFalse(any("/order/" in request.full_url for request in http.requests))

    def test_sell_availability_prevents_short_sale(self):
        http = FixtureHTTP(token(), current(), availability(sll_pbl_qty=4))
        adapter = self.adapter(http)
        with self.assertRaises(ProviderError):
            adapter.submit_limit(self.request(adapter, side="SELL"))
        self.assertEqual(json.loads(http.requests[-1].data)["Input_0"]["pcs_dit"], "3")
        self.assertFalse(any("/order/" in request.full_url for request in http.requests))

    def test_uncertain_order_http_or_transport_never_retries_or_refreshes(self):
        for outcome in (Response({}, status=401), Response({}, status=429), Response({}, status=503),
                        URLError("private fixture-secret"), Response(b"invalid-json")):
            http = FixtureHTTP(token(), current(), availability(), outcome)
            adapter = self.adapter(http)
            with self.subTest(outcome=type(outcome).__name__):
                result = adapter.submit_limit(self.request(adapter))
                self.assertEqual(result.status, "UNKNOWN")
                self.assertNotIn("fixture-secret", result.message)
                self.assertEqual(len(http.requests), 4)
                self.assertEqual(adapter.submit_limit(self.request(adapter)).status, "UNKNOWN")
                self.assertEqual(len(http.requests), 4)

    def test_calendar_and_observed_market_period_both_gate_submission(self):
        http = FixtureHTTP(token(), current("1"))
        adapter = self.adapter(http)
        with self.assertRaises(ProviderError):
            adapter.submit_limit(self.request(adapter))
        self.assertEqual(len(http.requests), 2)
        http = FixtureHTTP()
        adapter = self.adapter(http)
        with patch.object(self.nh, "_now", return_value=datetime(2026, 11, 26, 22, 0, tzinfo=timezone.utc)), self.assertRaises(ProviderError):
            adapter.submit_limit(self.request(adapter))
        self.assertEqual(http.requests, [])

    def test_invalid_quantities_prices_and_account_identity_fail_before_http(self):
        for fields in ({"quantity": True}, {"quantity": Decimal("5")}, {"quantity": 0},
                       {"limit_price": Decimal("49.123")}, {"limit_price": float("49.12")},
                       {"limit_price": Decimal("NaN")}, {"account_id": "other"}, {"symbol": "TQQQ/evil"}):
            http = FixtureHTTP()
            adapter = self.adapter(http)
            with self.subTest(fields=fields), self.assertRaises(ProviderError):
                adapter.submit_limit(self.request(adapter, **fields))
            self.assertEqual(http.requests, [])

    def test_order_status_reconciles_after_restart_and_keeps_fees_unknown(self):
        _, _, submitted = self.submit_fixture()
        http = FixtureHTTP(token(), orders(order_row(cns_qty=5, cns_pr="49.10", ny_cns_orr_qty=0,
                                                     oss_ato_orr_sts_cd="3")), orders())
        adapter = self.adapter(http)
        result = adapter.order_status(adapter.account_id, submitted.broker_order_id)
        self.assertEqual((result.status, result.filled_quantity, result.average_price), ("FILLED", Decimal(5), Decimal("49.10")))
        self.assertIsNone(result.commission_usd)
        self.assertIsNone(result.tax_usd)
        self.assertEqual([json.loads(r.data)["Input_0"]["orr_dt"] for r in http.requests[1:]], ["20261001", "20261002"])

    def test_order_status_ambiguity_or_correction_never_becomes_fill(self):
        _, _, submitted = self.submit_fixture()
        for responses in ((orders(order_row()), orders(order_row(orr_dt="20261002"))),
                          (orders(order_row(cor_qty=1)), orders()),
                          (orders(order_row(), order_row(orr_no=124, org_orr_no=123, cns_qty=1, cns_pr="49.1")), orders())):
            adapter = self.adapter(FixtureHTTP(token(), *responses))
            with self.subTest():
                self.assertEqual(adapter.order_status(adapter.account_id, submitted.broker_order_id).status, "UNKNOWN")

    def test_conflicting_duplicate_status_and_indirect_filled_lineage_remain_unknown(self):
        _, _, submitted = self.submit_fixture()
        cases = [orders(order_row(), order_row(cns_qty=5, cns_pr="49.1", ny_cns_orr_qty=0)),
                 orders(order_row(can_qty=5, ny_cns_orr_qty=0), order_row(orr_no=124, org_orr_no=123),
                        order_row(orr_no=125, org_orr_no=124, cns_qty=1, cns_pr="49.1"))]
        for response in cases:
            adapter = self.adapter(FixtureHTTP(token(), response, orders()))
            with self.subTest():
                self.assertEqual(adapter.order_status(adapter.account_id, submitted.broker_order_id).status, "UNKNOWN")

    def test_read_pagination_keeps_headers_and_never_sends_them_to_token(self):
        _, _, submitted = self.submit_fixture()
        http = FixtureHTTP(token(), orders(order_row(), headers={"cts": "page-two", "cts_flag": "Y"}),
                           orders(order_row(orr_no=124), headers={"cts_flag": "N"}), orders())
        adapter = self.adapter(http)
        result = adapter.order_status(adapter.account_id, submitted.broker_order_id)
        self.assertEqual(result.status, "SUBMITTED")
        self.assertEqual(http.requests[2].get_header("Cts"), "page-two")
        self.assertEqual(http.requests[2].get_header("Cts_flag"), "Y")
        self.assertIsNone(http.requests[0].get_header("Cts"))
        self.assertIs(adapter._provider._opener, http)

    def test_close_during_last_rate_limit_wait_prevents_transmission(self):
        http = FixtureHTTP(token(), current(), availability(), Response({"Output_0": {"orr_no": 123}}))
        adapter = self.adapter(http)
        waits = []
        with patch.object(self.nh, "_now", return_value=NOW) as clock:
            def advance_at_write(delay):
                waits.append(delay)
                if len(waits) == 2:
                    clock.return_value = datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)
            with patch.object(self.nh.time, "sleep", side_effect=advance_at_write):
                result = adapter.submit_limit(self.request(adapter))
        self.assertEqual(result.status, "UNKNOWN")
        self.assertFalse(any("/order/" in request.full_url for request in http.requests))

    def test_external_stop_during_final_wait_prevents_order_transmission(self):
        http = FixtureHTTP(token(), current(), availability(), Response({"Output_0": {"orr_no": 123}}))
        adapter = self.adapter(http)
        event = threading.Event()
        adapter.set_stop_event(event)
        waits = []
        def stop_at_write(delay):
            waits.append(delay)
            if len(waits) == 2:
                event.set()
        with patch.object(self.nh.time, "sleep", side_effect=stop_at_write):
            result = adapter.submit_limit(self.request(adapter))
        self.assertEqual(result.status, "UNKNOWN")
        self.assertFalse(any("/order/" in request.full_url for request in http.requests))

    def test_cancel_ack_is_not_final_cancellation_and_is_not_repeated(self):
        http, adapter, submitted = self.submit_fixture(orders(order_row()), orders(), Response({"Output_0": {"orr_no": 456}}))
        result = adapter.cancel(adapter.account_id, submitted.broker_order_id)
        self.assertEqual(result.status, "SUBMITTED")
        self.assertTrue(result.cancel_requested)
        self.assertTrue(result.related_order_ids)
        self.assertEqual(json.loads(http.requests[-1].data), {"Input_0": {"act_no": "12345678901", "org_orr_no": 123,
            "fc_sec_trd_nat_cd": "200", "iem_cd": "TQQQ", "all_pat_dit_cd": "1"}})
        before = len(http.requests)
        self.assertEqual(adapter.cancel(adapter.account_id, submitted.broker_order_id), result)
        self.assertEqual(len(http.requests), before)

    def test_confirmed_partial_cancellation_keeps_actual_filled_quantity(self):
        _, _, submitted = self.submit_fixture()
        adapter = self.adapter(FixtureHTTP(token(), orders(order_row(cns_qty=2, cns_pr="49.1", can_qty=3,
            ny_cns_orr_qty=0, oss_ato_orr_sts_cd="3")), orders()))
        result = adapter.order_status(adapter.account_id, submitted.broker_order_id)
        self.assertEqual((result.status, result.filled_quantity), ("CANCELLED", Decimal(2)))

    def test_snapshot_checks_positions_cash_pending_orders_and_session(self):
        balance = Response({"Output_0": {"fc_dca": "1000.125"}, "Output_1": [{"fc_sec_trd_nat_cd": "200",
            "cur_cd": "USD", "iem_cd": "TQQQ", "cns_bse_bnc_qty": 10, "cfd_lon_cd_nm": "현금", "lon_dt": "", "xrn_dt": ""}]})
        http = FixtureHTTP(token(), balance, orders(), orders(), current())
        adapter = self.adapter(http)
        snapshot = adapter.snapshot(adapter.account_id, ("TQQQ",))
        self.assertEqual(snapshot.holdings, {"TQQQ": Decimal(10)})
        self.assertEqual(snapshot.available_usd, Decimal("1000.125"))
        self.assertTrue(snapshot.data_verified)
        self.assertTrue(snapshot.market_open)
        self.assertEqual(snapshot.market_close_at, datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc))

    def test_redirect_is_blocked_without_echoing_credentials(self):
        response = current()
        response.url = "https://evil.invalid/fixture-secret"
        adapter = self.adapter(FixtureHTTP(token(), response))
        with self.assertRaises(ProviderError) as caught:
            adapter.quote(adapter.account_id, "TQQQ")
        self.assertNotIn("fixture-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
