import hashlib
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from stockjournal import providers
from stockjournal.providers import NHProvider, ProviderError, load_nh_master, parse_nh_master


def master_row(code="005930", market="1", name="삼성전자", venture="0", sector="KGS11P"):
    row = bytearray(b" " * 236)
    row[0:6] = code.encode("ascii")
    row[6:7] = market.encode("ascii")
    encoded_name = name.encode("cp949")
    row[7:48] = encoded_name.ljust(41, b" ")
    row[165:166] = venture.encode("ascii")
    row[199:205] = sector.encode("ascii").ljust(6, b" ")
    return bytes(row) + b"\n"


class Response:
    def __init__(self, data, headers=None, status=200, url=None):
        self.body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.headers = headers or {}
        self.status = status
        self.url = url

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FixtureHTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("Unexpected extra HTTP request")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if item.url is None:
            item.url = request.full_url
        return item


def token(value="test-token", **extra):
    return Response({"access_token": value, "token_type": "Bearer", "expires_in": 86400, **extra})


def quote(value="70000"):
    return Response({"rsp_cd": "00000", "rsp_msg": "정상 조회되었습니다.", "Output_0": {"stck_prpr": value}})


def trade_row(order="0000000002", quantity="3", price="100", amount="300", **extra):
    return {
        "itg_orr_no": order, "org_itg_orr_no": "0", "iem_cd": "005930", "iem_nm": "삼성전자",
        "sby_dit_cd_nm": "현금매수", "tot_cns_qty": quantity, "cns_avg_uit_pr": price,
        "cns_amt": amount, "cfd_lon_cd": "00", **extra,
    }


def trade_page(rows, headers=None, message="정상 조회되었습니다.", code="00000"):
    return Response({"rsp_cd": code, "rsp_msg": message, "Output_0": rows}, headers=headers)


INSTRUMENT = {"id": "KR:005930", "market": "KR", "code": "005930", "name": "삼성전자"}


class MasterTests(unittest.TestCase):
    def test_cp949_byte_offsets_etf_etn_and_alphanumeric_codes(self):
        raw = (master_row(name="*삼성전자") + master_row("0000D0", name="TIGER ETF", venture="8")
               + master_row("500029", market="A", name="신한 ETN", venture="E"))
        rows = parse_nh_master(raw)
        self.assertEqual([(r["id"], r["name"], r["asset_type"]) for r in rows], [
            ("KR:005930", "삼성전자", "주식"), ("KR:0000D0", "TIGER ETF", "ETF"),
            ("KR:500029", "신한 ETN", "ETN"),
        ])
        self.assertEqual(rows[0]["sector"], "KGS11P")
        self.assertEqual(rows[0]["currency"], "KRW")
        self.assertEqual(rows[0]["market"], "KR")
        self.assertFalse(rows[0]["deleted"])
        self.assertIsNotNone(datetime.fromisoformat(rows[0]["updated_at"]).tzinfo)

    def test_venture_flag_only_marks_etf_in_market_one(self):
        self.assertEqual(parse_nh_master(master_row(market="4", venture="8"))[0]["asset_type"], "주식")

    def test_crlf_and_missing_final_newline(self):
        self.assertEqual(len(parse_nh_master(master_row().replace(b"\n", b"\r\n"))), 1)
        self.assertEqual(len(parse_nh_master(master_row().rstrip(b"\n"))), 1)

    def test_corrupt_record_never_returns_partial_master(self):
        valid = master_row()
        bad_encoding = bytearray(master_row("000660"))
        bad_encoding[7:9] = b"\xff\xff"
        for raw in (b"", valid + b"short\n", valid + b"\n", valid + valid,
                    bytes(bad_encoding), master_row("00/930"), master_row(name=""), valid[:-2]):
            with self.subTest(raw_length=len(raw)), self.assertRaises(ProviderError):
                parse_nh_master(raw)

    def test_loader_downloads_only_public_https_master(self):
        http = FixtureHTTP(Response(master_row()))
        with patch.object(providers, "build_opener", return_value=http):
            self.assertEqual(load_nh_master()[0]["code"], "005930")
        request = http.requests[0]
        self.assertEqual(request.full_url, "https://www.nhplug.com/instruments/m_new_stock.mst")
        self.assertEqual(request.get_method(), "GET")
        self.assertNotIn("Authorization", dict(request.header_items()))


class NHProviderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache_dir = Path(self.directory.name) / "cache"
        self.case_counter = 0

    def provider(self, http, key="fixture-key", secret="fixture-secret", *, reuse_cache=False):
        self.case_counter += 1
        cache_dir = self.cache_dir if reuse_cache else Path(self.directory.name) / f"case-{self.case_counter}"
        with patch.object(providers, "build_opener", return_value=http):
            return NHProvider(key, secret, cache_dir)

    def test_token_query_body_and_quote_headers_match_documented_contract(self):
        http = FixtureHTTP(token(), quote("70,000.25"))
        actual = self.provider(http).quotes([INSTRUMENT])
        self.assertEqual(actual["KR:005930"]["price"], "70000.25")
        self.assertEqual(actual["KR:005930"]["source"], "NH polling")
        self.assertIsNotNone(datetime.fromisoformat(actual["KR:005930"]["as_of"]).tzinfo)
        auth, query = http.requests
        self.assertEqual((urlsplit(auth.full_url).scheme, urlsplit(auth.full_url).netloc), ("https", "api.nhplug.com:8443"))
        self.assertEqual(urlsplit(auth.full_url).path, "/oauth2/token")
        self.assertEqual(parse_qs(urlsplit(auth.full_url).query), {
            "appkey": ["fixture-key"], "appsecretkey": ["fixture-secret"],
            "grant_type": ["client_credentials"], "scope": ["oob"],
        })
        self.assertEqual(auth.get_method(), "POST")
        self.assertEqual(auth.data, b"")
        self.assertEqual(auth.get_header("Content-type"), "application/x-www-form-urlencoded")
        self.assertEqual(query.full_url, "https://api.nhplug.com:8443/krstock/quote/v1/currentPrice")
        self.assertEqual(json.loads(query.data), {"Input_0": {"iem_cd": "005930", "market_cd": "UNT"}})
        headers = {k.lower(): v for k, v in query.header_items()}
        self.assertEqual(headers["authorization"], "Bearer test-token")
        self.assertNotIn("x-client-id", headers)
        self.assertNotIn("x-client-secret", headers)

    def test_quotes_skip_unsupported_markets_without_an_auth_request(self):
        http = FixtureHTTP()
        self.assertEqual(self.provider(http).quotes([{"id": "JP:7203", "market": "JP", "code": "7203"}]), {})
        self.assertEqual(http.requests, [])

    def test_us_quotes_use_documented_ticker_and_currency(self):
        http = FixtureHTTP(token(), Response({"Output_0": {"iem_cd": "TQQQ", "trdprc": "49.125",
                                                           "currency_unit": "USD", "trade_date": "20261001"}}))
        actual = self.provider(http).quotes([{"id": "US:TQQQ", "market": "US", "code": "TQQQ", "currency": "USD"}])
        self.assertEqual(actual["US:TQQQ"]["price"], "49.125")
        self.assertEqual(actual["US:TQQQ"]["currency"], "USD")
        self.assertEqual(http.requests[1].full_url, "https://api.nhplug.com:8443/gbstock/quote/v1/current")
        self.assertEqual(json.loads(http.requests[1].data), {"Input_0": {"iem_cd": "TQQQ"}})

    def test_us_quote_rejects_mismatched_symbol_or_currency(self):
        for change in ({"iem_cd": "AAPL"}, {"currency_unit": "JPY"}):
            output = {"iem_cd": "TQQQ", "trdprc": "50", "currency_unit": "USD", **change}
            with self.subTest(change=change), self.assertRaises(ProviderError):
                self.provider(FixtureHTTP(token(), Response({"Output_0": output}))).quotes([
                    {"id": "US:TQQQ", "market": "US", "code": "TQQQ", "currency": "USD"}])

    def test_discovery_exposes_live_mock_and_unknown_accounts_without_persisting_numbers(self):
        http = FixtureHTTP(token(), Response({"rsp_msg": "조회가 완료되었습니다.", "cust_no": "private-customer",
            "Output_0": [{"acct_no": "12345678901", "acct_type": "01"},
                         {"acct_no": "12345678902", "acct_type": "03"},
                         {"acct_no": "12345678903", "acct_type": "99"}]}))
        provider = self.provider(http)
        actual = provider.discover_accounts()
        self.assertEqual([(a["account_type"], a["environment"], a["usable"]) for a in actual],
                         [("01", "live", True), ("03", "mock", False), ("99", "unknown", False)])
        self.assertEqual(actual[0]["account_number"], "12345678901")
        self.assertEqual(actual[0]["masked_number"], "*******8901")
        self.assertNotIn("private-customer", json.dumps(actual))
        self.assertEqual(http.requests[1].full_url, "https://api.nhplug.com:8443/n2/acctinfo")
        self.assertEqual(json.loads(http.requests[1].data), {"Input_0": {}})
        cache_files = list(Path(self.directory.name).rglob("*.bin"))
        self.assertEqual(cache_files, [provider._secrets.path] if provider._secrets.supported else [])
        self.assertEqual(set(provider._secrets._values), {"token"})
        cache_contents = json.dumps(provider._secrets._values)
        for sensitive in ("12345678901", "12345678902", "12345678903", "private-customer"):
            self.assertNotIn(sensitive, cache_contents)
        for path in cache_files:
            protected = path.read_bytes()
            for sensitive in (b"test-token", b"fixture-secret", b"12345678901", b"private-customer"):
                self.assertNotIn(sensitive, protected)

    def test_account_discovery_does_not_hide_malformed_or_ambiguous_rows(self):
        for rows in ([{"acct_no": "bad", "acct_type": "01"}],
                     [{"acct_no": "12345678901", "acct_type": "01"}, {"acct_no": "12345678901", "acct_type": "03"}], []):
            with self.subTest(rows=rows), self.assertRaises(ProviderError):
                self.provider(FixtureHTTP(token(), Response({"Output_0": rows}))).discover_accounts()

    def test_common_message_envelope_preserves_empty_result_and_rejects_errors(self):
        http = FixtureHTTP(token(), Response({"message": {"code": "NO_ROWS", "msg": "조회내역이 없습니다."}}))
        self.assertEqual(self.provider(http).discover_accounts(), [])
        http = FixtureHTTP(token(), Response({"message": {"code": "BAD", "msg": "계좌 권한 오류 fixture-secret"},
            "Output_0": [{"acct_no": "12345678901", "acct_type": "01"}]}))
        with self.assertRaises(ProviderError) as caught:
            self.provider(http).discover_accounts()
        self.assertNotIn("fixture-secret", str(caught.exception))

    def test_domestic_holdings_collect_pages_and_only_use_final_summary(self):
        row = {"iem_cd": "005930", "iem_nm": "삼성전자", "tp_cd_nm": "현금", "itg_bnc_qty": "3",
               "phs_pr": "70000", "now_pr": "69000", "eal_amt": "207000", "eal_pls_amt": "-3000"}
        http = FixtureHTTP(token(), Response({"Output_0": {"tot_eal_amt": 0}, "Output_1": [row]},
                                            {"cts": "holdings-next", "cts_flag": "Y"}),
                           Response({"Output_0": {"dca": "5000", "tot_eal_amt": "207000", "tot_eal_pls": "-3000"},
                                     "Output_1": [row]}, {"cts_flag": "N"}))
        snapshot = self.provider(http).get_holdings("12345678901")
        self.assertEqual(len(snapshot["holdings"]), 1)
        position = snapshot["holdings"][0]
        self.assertEqual((position["instrument_id"], position["quantity"], position["average_price"], position["market_value"],
                          position["unrealized_pnl"]), ("KR:005930", "3", "70000", "207000", "-3000"))
        self.assertIsNone(position["book_value"])
        self.assertEqual(snapshot["summary"]["tot_eal_amt"], "207000")
        self.assertNotIn("12345678901", json.dumps(snapshot))
        self.assertEqual(json.loads(http.requests[1].data), {"Input_0": {
            "act_no": "12345678901", "bnc_bse_cd": "5", "ltg_aot_dit_cd": "9", "aet_bse": "2",
            "qut_dit_cd": "UNT", "aly_qut_cd": "1"}})
        self.assertEqual(http.requests[2].get_header("Cts"), "holdings-next")

    def test_us_holdings_preserve_broker_cost_and_finite_decimal_values(self):
        row = {"fc_sec_trd_nat_cd": "200", "iem_cd": "TQQQ", "iem_nm": "PROSHARES ULTRAPRO QQQ",
               "cns_bse_bnc_qty": "7", "fc_phs_uit_pr": "40.125", "fc_sec_end_pr": "42.25",
               "fc_eal_amt": "295.75", "fc_abk_amt": "280.875", "fc_eal_pls_amt": "14.875",
               "cur_cd": "USD", "cfd_lon_cd_nm": "현금"}
        http = FixtureHTTP(token(), Response({"Output_0": {"fc_dca": "100.125"}, "Output_1": [row]}))
        snapshot = self.provider(http).get_holdings("12345678901", overseas=True)
        position = snapshot["holdings"][0]
        self.assertEqual((position["instrument_id"], position["market"], position["currency"], position["book_value"]),
                         ("US:TQQQ", "US", "USD", "280.875"))
        self.assertEqual(snapshot["summary"]["fc_dca"], "100.125")
        self.assertEqual(json.loads(http.requests[1].data), {"Input_0": {"act_no": "12345678901",
            "qut_iqr_dit_cd": "9", "fc_sec_trd_nat_cd": "200", "cur_cd": "USD", "xns_dit_cd": "1"}})

    def test_holdings_reject_incomplete_pages_conflicting_positions_and_nested_errors(self):
        row = {"iem_cd": "005930", "iem_nm": "삼성전자", "tp_cd_nm": "현금", "itg_bnc_qty": "3", "phs_pr": "10"}
        for pages in ([Response({"Output_1": [row]}, {"cts_flag": "Y"})],
                      [Response({"Output_1": [row, {**row, "itg_bnc_qty": "4"}]})],
                      [Response({"Output_1": [row], "message": {"msg_code": "BAD", "usr_msg": "조회 권한이 없습니다."}})],
                      [Response({"Output_1": []})]):
            with self.subTest(), self.assertRaises(ProviderError):
                self.provider(FixtureHTTP(token(), *pages)).get_holdings("12345678901")

    def test_explicit_empty_holdings_preserve_cash_summary(self):
        http = FixtureHTTP(token(), Response({"message": {"usr_msg": "조회 내역이 없습니다."}, "Output_0": {"dca": "100"}}))
        snapshot = self.provider(http).get_holdings("12345678901")
        self.assertEqual(snapshot["holdings"], [])
        self.assertEqual(snapshot["summary"], {"dca": "100"})

    def test_overseas_cash_orders_map_to_existing_trade_schema(self):
        row = {"orr_no": 12, "org_orr_no": 0, "oss_sby_dit_cd": "2", "sby_dit_nm": "매수",
               "fc_sec_trd_nat_cd": "200", "iem_cd": "TQQQ", "iem_nm": "PROSHARES ULTRAPRO QQQ",
               "cns_qty": 5, "cns_pr": "47.123456", "cfd_lon_cd": "00", "orr_dt": "20261001",
               "cor_qty": 0, "can_qty": 0}
        http = FixtureHTTP(token(), trade_page([row]))
        actual = self.provider(http).overseas_trades("12345678901", "2026-10-01", "2026-10-01")
        self.assertEqual(len(actual), 1)
        actual = actual[0]
        self.assertEqual((actual["instrument_id"], actual["quantity"], actual["price"], actual["side"], actual["currency"]),
                         ("US:TQQQ", "5", "47.123456", "BUY", "USD"))
        self.assertEqual((actual["_instrument_market"], actual["_instrument_code"], actual["_instrument_currency"]),
                         ("US", "TQQQ", "USD"))
        self.assertTrue(actual["id"].startswith("nh-us:"))
        self.assertNotIn("12345678901", json.dumps(actual))
        self.assertEqual(json.loads(http.requests[1].data), {"Input_0": {"orr_dt": "20261001", "act_no": "12345678901",
                         "oss_sby_dit_cd": "0", "sot_dit": "0", "ost_cns_dit": "1"}})

    def test_overseas_orders_reject_corrections_non_us_credit_and_date_conflicts(self):
        row = {"orr_no": 1, "org_orr_no": 0, "oss_sby_dit_cd": "2", "fc_sec_trd_nat_cd": "200",
               "iem_cd": "TQQQ", "cns_qty": 1, "cns_pr": "50", "cfd_lon_cd": "00", "orr_dt": "20261001"}
        for changes in ({"org_orr_no": 1}, {"cor_qty": 1}, {"can_qty": 1}, {"fc_sec_trd_nat_cd": "120"},
                        {"cfd_lon_cd": "03"}, {"oss_sby_dit_cd": "0"}, {"orr_dt": "20261002"},
                        {"org_orr_no": False}, {"cor_qty": False}, {"can_qty": False}):
            with self.subTest(changes=changes), self.assertRaises(ProviderError):
                self.provider(FixtureHTTP(token(), trade_page([{**row, **changes}]))).overseas_trades(
                    "12345678901", "2026-10-01", "2026-10-01")

    def test_daily_prices_are_historical_regular_sessions_with_unknown_adjustment(self):
        rows = [{"trade_date": "20261002", "open_prc": "49", "high": "52", "low": "48", "close_prc": "51", "movolume": 100},
                {"trade_date": "20261001", "open_prc": "48", "high": "50", "low": "47", "close_prc": "49", "movolume": 90}]
        http = FixtureHTTP(token(), Response({"Output_1": rows}))
        actual = self.provider(http).overseas_daily_prices("TQQQ", "2026-10-02", count=260)
        self.assertEqual([row["date"] for row in actual["rows"]], ["2026-10-01", "2026-10-02"])
        self.assertEqual(actual["rows"][0]["close"], "49")
        self.assertEqual((actual["session"], actual["adjustment"], actual["currency"]), ("regular", "unknown", "USD"))
        self.assertTrue(actual["warnings"])
        self.assertEqual(json.loads(http.requests[1].data), {"Input_0": {"iem_cd": "TQQQ", "end_dt": "20261002",
            "count": "0260", "maxavg": "200", "gubun": "3", "xtick": "0001", "today_cls": "0", "market_cls": "1"}})

    def test_daily_prices_reject_duplicates_invalid_ohlc_and_dates_after_cutoff(self):
        row = {"trade_date": "20261001", "open_prc": "49", "high": "51", "low": "48", "close_prc": "50", "movolume": 100}
        for rows in ([row, row], [{**row, "high": "49"}], [{**row, "trade_date": "20261003"}],
                     [{**row, "trade_date": "20261301"}], [{**row, "close_prc": "NaN"}]):
            with self.subTest(rows=rows), self.assertRaises(ProviderError):
                self.provider(FixtureHTTP(token(), Response({"Output_1": rows}))).overseas_daily_prices("TQQQ", "2026-10-02")

    def test_daily_prices_use_conservative_new_york_close_and_weekend_cutoff(self):
        cases = [(datetime(2026, 10, 2, 20, 9, tzinfo=timezone.utc), "2026-10-01"),
                 (datetime(2026, 10, 2, 20, 10, tzinfo=timezone.utc), "2026-10-02"),
                 (datetime(2026, 10, 4, 21, 0, tzinfo=timezone.utc), "2026-10-02")]
        for now, expected in cases:
            http = FixtureHTTP(token(), Response({"rsp_msg": "조회내역이 없습니다."}))
            with self.subTest(now=now), patch.object(providers, "datetime") as clock:
                clock.now.return_value = now
                snapshot = self.provider(http).overseas_daily_prices("TQQQ", "2026-10-04")
            self.assertEqual(snapshot["end_date"], expected)
            self.assertEqual(snapshot["finalization"], "completed_sessions")
            self.assertEqual(json.loads(http.requests[1].data)["Input_0"]["end_dt"], expected.replace("-", ""))

    def test_daily_prices_respect_published_holiday_and_early_close(self):
        cases = [(datetime(2026, 11, 26, 22, 0, tzinfo=timezone.utc), "2026-11-25"),
                 (datetime(2026, 11, 27, 18, 10, tzinfo=timezone.utc), "2026-11-27")]
        for now, expected in cases:
            http = FixtureHTTP(token(), Response({"rsp_msg": "조회내역이 없습니다."}))
            with self.subTest(now=now), patch.object(providers, "datetime") as clock:
                clock.now.return_value = now
                snapshot = self.provider(http).overseas_daily_prices("TQQQ")
            self.assertEqual(snapshot["end_date"], expected)
            self.assertEqual(snapshot["finalization_policy"], "published_calendar_close_plus_10m")

    def test_queries_throttle_before_followup_without_reissuing_token(self):
        http = FixtureHTTP(token(), quote(), quote())
        provider = self.provider(http)
        with patch.object(providers.time, "monotonic", return_value=50), patch.object(providers.time, "sleep") as sleep:
            provider.quotes([INSTRUMENT])
            provider.quotes([INSTRUMENT])
        self.assertEqual(len(http.requests), 3)
        self.assertEqual(sleep.call_args.args[0], 0.25)

    def test_quotes_reject_invalid_or_nonpositive_prices(self):
        for value in ("0", "-5", "NaN", "Infinity", "", None, "7,00", True):
            with self.subTest(value=value), self.assertRaises(ProviderError):
                self.provider(FixtureHTTP(token(), quote(value))).quotes([INSTRUMENT])

    def test_quotes_missing_output_or_business_error_is_not_silently_empty(self):
        for response in ({"rsp_cd": "bad", "rsp_msg": "인증 실패 fixture-secret"},
                         {"Output_0": {}}, {"Output_0": {"stck_prpr": "70000"}, "error_code": "INVALID"}):
            with self.subTest(response=response), self.assertRaises(ProviderError) as caught:
                self.provider(FixtureHTTP(token(), Response(response))).quotes([INSTRUMENT])
            self.assertNotIn("fixture-secret", str(caught.exception))

    def test_401_refreshes_once_and_429_keeps_the_same_token(self):
        http = FixtureHTTP(token("one"), Response({}, status=401), token("two"),
                           Response({}, status=429, headers={"Retry-After": "999999"}), quote())
        with patch.object(providers.time, "sleep") as sleep:
            actual = self.provider(http).quotes([INSTRUMENT])
        self.assertEqual(actual["KR:005930"]["price"], "70000")
        self.assertEqual(sum(urlsplit(r.full_url).path == "/oauth2/token" for r in http.requests), 2)
        self.assertEqual([r.get_header("Authorization") for r in http.requests if r.data],
                         ["Bearer one", "Bearer two", "Bearer two"])
        self.assertLessEqual(sleep.call_args.args[0], 2)

    def test_retries_are_bounded_on_repeated_401_or_429(self):
        for http in (
            FixtureHTTP(token(), Response({}, status=401), token(), Response({}, status=401)),
            FixtureHTTP(token(), *(Response({}, status=429) for _ in range(3))),
        ):
            with self.subTest(), patch.object(providers.time, "sleep"), self.assertRaises(ProviderError):
                self.provider(http).quotes([INSTRUMENT])
            self.assertEqual(len(http.requests), 4)

    def test_network_http_and_json_errors_never_echo_credentials(self):
        cases = [URLError("fixture-secret fixture-key"),
                 HTTPError("https://bad/?appsecretkey=fixture-secret", 403, "fixture-key", {}, io.BytesIO(b"fixture-secret")),
                 Response(b'{"raw":"fixture-secret"'), Response({}, status=302)]
        for error in cases:
            with self.subTest(error=type(error).__name__), self.assertRaises(ProviderError) as caught:
                self.provider(FixtureHTTP(error)).quotes([INSTRUMENT])
            self.assertNotIn("fixture-key", str(caught.exception))
            self.assertNotIn("fixture-secret", str(caught.exception))

    def test_credentials_and_server_redirect_cannot_change_request_destination(self):
        for key, secret in (("bad\r\nInjected: x", "ok"), ("ok", "bad\n"), ("", "ok")):
            with self.subTest(), self.assertRaises(ProviderError):
                self.provider(FixtureHTTP(), key, secret)
        http = FixtureHTTP(Response({"access_token": "no"}, url="https://example.com/steal"))
        with self.assertRaises(ProviderError):
            self.provider(http).quotes([INSTRUMENT])
        no_redirect = providers._NoRedirect()
        self.assertIsNone(no_redirect.redirect_request(http.requests[0], None, 302, "", {}, "https://example.com"))

    def test_encrypted_cache_reuses_token_but_never_beyond_24_hours(self):
        encrypt = lambda raw: bytes(value ^ 0xA5 for value in raw)
        with patch("stockjournal.secrets.sys", SimpleNamespace(platform="win32")), \
             patch("stockjournal.secrets._dpapi_encrypt", side_effect=encrypt), \
             patch("stockjournal.secrets._dpapi_decrypt", side_effect=encrypt):
            with patch.object(providers.time, "time", return_value=100000):
                self.provider(FixtureHTTP(token("private-token", expires_in=999999), quote()), reuse_cache=True).quotes([INSTRUMENT])
            saved = next(self.cache_dir.iterdir()).read_bytes()
            self.assertNotIn(b"private-token", saved)
            self.assertNotIn(b"fixture-secret", saved)
            before = FixtureHTTP(quote())
            with patch.object(providers.time, "time", return_value=186300):
                self.provider(before, reuse_cache=True).quotes([INSTRUMENT])
            self.assertEqual(len(before.requests), 1)
            after = FixtureHTTP(token("refreshed"), quote())
            with patch.object(providers.time, "time", return_value=186401):
                self.provider(after, reuse_cache=True).quotes([INSTRUMENT])
            self.assertEqual(len(after.requests), 2)

    def test_daily_trade_request_and_numeric_order_sequence(self):
        http = FixtureHTTP(token(), trade_page([trade_row("10"), trade_row("2", sby_dit_cd_nm="매도")]))
        rows = self.provider(http).trades("12345678901", "2026-10-01", "2026-10-01")
        self.assertEqual([r["sequence"] for r in rows], [2, 10])
        self.assertEqual([r["side"] for r in rows], ["SELL", "BUY"])
        self.assertEqual(rows[0]["executed_at"], "2026-10-01")
        self.assertEqual(rows[0]["account_id"], "")
        self.assertEqual(rows[0]["currency"], "KRW")
        self.assertEqual((rows[0]["quantity"], rows[0]["price"], rows[0]["fee"], rows[0]["tax"]), ("3", "100", "0", "0"))
        account_hash = hashlib.sha256(b"12345678901").hexdigest()[:24]
        self.assertEqual(rows[0]["id"], f"nh:{account_hash}:2026-10-01:2")
        self.assertEqual(rows[0]["external_id"], rows[0]["id"])
        self.assertNotIn("12345678901", json.dumps(rows))
        self.assertEqual(rows[0]["_instrument_name"], "삼성전자")
        self.assertTrue(rows[0]["_warnings"])
        self.assertEqual(json.loads(http.requests[1].data), {"Input_0": {
            "orr_dt": "20261001", "act_no": "12345678901", "orr_mkt_cd": "00", "ost_cns_dit": "1",
        }})

    def test_trade_amount_overrides_rounded_average_with_high_precision(self):
        http = FixtureHTTP(token(), trade_page([trade_row(quantity="3", price="33", amount="100")]))
        row = self.provider(http).trades("12345678901", date(2026, 10, 1), date(2026, 10, 1))[0]
        self.assertLess(abs(Decimal(row["price"]) - Decimal("33.33333333333333333333333333")), Decimal("0.00000000000000000000000001"))
        self.assertGreater(len(row["price"]), 40)

    def test_trade_average_is_used_only_when_amount_is_absent(self):
        original = trade_row(price="123.45")
        original.pop("cns_amt")
        rows = self.provider(FixtureHTTP(token(), trade_page([original]))).trades("12345678901", "2026-10-01", "2026-10-01")
        self.assertEqual(rows[0]["price"], "123.45")
        for invalid in ("bad", "0", "-1", "NaN"):
            with self.subTest(invalid=invalid), self.assertRaises(ProviderError):
                self.provider(FixtureHTTP(token(), trade_page([trade_row(amount=invalid)]))).trades("12345678901", "2026-10-01", "2026-10-01")

    def test_pagination_keeps_highest_cumulative_quantity_and_ignores_unfilled(self):
        http = FixtureHTTP(token(),
                           trade_page([trade_row(quantity="1", amount="100")], {"cts": "next-page", "cts_flag": "Y"}),
                           trade_page([trade_row(quantity="3", amount="315"), trade_row("3", quantity="0", amount="0")],
                                      {"cts_flag": "N"}))
        rows = self.provider(http).trades("12345678901", "2026-10-01", "2026-10-01")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["quantity"], rows[0]["price"]), ("3", "105"))
        headers = {k.lower(): v for k, v in http.requests[2].header_items()}
        self.assertEqual(headers["cts"], "next-page")
        self.assertEqual(headers["cts_flag"], "Y")

    def test_legacy_continuation_code_without_flag_fetches_the_next_page(self):
        http = FixtureHTTP(token(), trade_page([trade_row()], {"cts": "cursor"}, code="00165"),
                           trade_page([trade_row("3")], {"cts": "cursor", "cts_flag": "N"}))
        self.assertEqual(len(self.provider(http).trades("12345678901", "2026-10-01", "2026-10-01")), 2)

    def test_equal_quantity_price_conflict_or_order_identity_conflict_rejects_batch(self):
        for conflicting in (trade_row(amount="301"), trade_row(iem_cd="000660"), trade_row(sby_dit_cd_nm="매도")):
            with self.subTest(conflicting=conflicting), self.assertRaises(ProviderError):
                self.provider(FixtureHTTP(token(), trade_page([trade_row(), conflicting]))).trades("12345678901", "2026-10-01", "2026-10-01")

    def test_credit_amended_unknown_side_and_malformed_orders_reject_batch(self):
        for changes in ({"cfd_lon_cd": "03"}, {"org_itg_orr_no": "2"}, {"sby_dit_cd_nm": "신용매수"},
                        {"itg_orr_no": "bad"}, {"itg_orr_no": "0"}, {"iem_cd": "005930/"},
                        {"tot_cns_qty": "NaN"}, {"tot_cns_qty": "-1"}):
            with self.subTest(changes=changes), self.assertRaises(ProviderError):
                self.provider(FixtureHTTP(token(), trade_page([trade_row(), trade_row(**changes)]))).trades("12345678901", "2026-10-01", "2026-10-01")

    def test_only_explicit_no_records_response_can_be_an_empty_trade_result(self):
        http = FixtureHTTP(token(), trade_page([], message="조회내역이 없습니다."))
        self.assertEqual(self.provider(http).trades("12345678901", "2026-10-01", "2026-10-01"), [])
        for response in (trade_page([]), trade_page([], message="인증 실패"), Response({}), trade_page(None)):
            with self.subTest(), self.assertRaises(ProviderError):
                self.provider(FixtureHTTP(token(), response)).trades("12345678901", "2026-10-01", "2026-10-01")

    def test_pagination_cycle_or_missing_continuation_key_rejects_partial_result(self):
        for pages in (
            [trade_page([trade_row()], {"cts_flag": "Y"})],
            [trade_page([trade_row()], {"cts": "same", "cts_flag": "Y"}),
             trade_page([trade_row()], {"cts": "same", "cts_flag": "Y"})],
        ):
            with self.subTest(), self.assertRaises(ProviderError):
                self.provider(FixtureHTTP(token(), *pages)).trades("12345678901", "2026-10-01", "2026-10-01")

    def test_date_range_is_inclusive_and_invalid_ranges_do_not_authenticate(self):
        http = FixtureHTTP(token(), trade_page([], message="조회내역이 없습니다."), trade_page([], message="조회내역이 없습니다."))
        self.provider(http).trades("12345678901", "2026-10-01", "2026-10-02")
        self.assertEqual([json.loads(r.data)["Input_0"]["orr_dt"] for r in http.requests if r.data], ["20261001", "20261002"])
        for start, end in (("bad", "2026-10-01"), ("2026-10-02", "2026-10-01"), ("2024-01-01", "2026-01-01")):
            unused = FixtureHTTP()
            with self.subTest(), self.assertRaises(ProviderError):
                self.provider(unused).trades("12345678901", start, end)
            self.assertEqual(unused.requests, [])


if __name__ == "__main__":
    unittest.main()
