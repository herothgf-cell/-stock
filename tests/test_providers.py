import hashlib
import io
import json
import tempfile
import unittest
from datetime import date, datetime
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

    def provider(self, http, key="fixture-key", secret="fixture-secret"):
        with patch.object(providers, "build_opener", return_value=http):
            return NHProvider(key, secret, self.cache_dir)

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
        self.assertEqual(headers["x-client-id"], "fixture-key")
        self.assertEqual(headers["x-client-secret"], "fixture-secret")

    def test_quotes_skip_foreign_instruments_without_an_auth_request(self):
        http = FixtureHTTP()
        self.assertEqual(self.provider(http).quotes([{"id": "US:AAPL", "market": "US", "code": "AAPL"}]), {})
        self.assertEqual(http.requests, [])

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
                self.provider(FixtureHTTP(token("private-token", expires_in=999999), quote())).quotes([INSTRUMENT])
            saved = next(self.cache_dir.iterdir()).read_bytes()
            self.assertNotIn(b"private-token", saved)
            self.assertNotIn(b"fixture-secret", saved)
            before = FixtureHTTP(quote())
            with patch.object(providers.time, "time", return_value=186300):
                self.provider(before).quotes([INSTRUMENT])
            self.assertEqual(len(before.requests), 1)
            after = FixtureHTTP(token("refreshed"), quote())
            with patch.object(providers.time, "time", return_value=186401):
                self.provider(after).quotes([INSTRUMENT])
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
