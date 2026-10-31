"""Read-only NH PLUG adapters using Python's standard library.

All requests use fixed HTTPS destinations; redirects and raw server error
messages are prohibited because token URLs contain application credentials.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import ssl
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from zoneinfo import ZoneInfoNotFoundError

from .secrets import SecretStore


MASTER_URL = "https://www.nhplug.com/instruments/m_new_stock.mst"
API_URL = "https://api.nhplug.com:8443"
_QUOTE_PATH = "/krstock/quote/v1/currentPrice"
_TRADES_PATH = "/krstock/inquiry/v1/dailyOrderExecution"
_ACCOUNTS_PATH = "/n2/acctinfo"
_BALANCE_PATH = "/krstock/inquiry/v1/balance"
_US_BALANCE_PATH = "/gbstock/inquiry/v1/balance"
_US_QUOTE_PATH = "/gbstock/quote/v1/current"
_US_TRADES_PATH = "/gbstock/inquiry/v1/unexecuted"
_US_DAILY_PATH = "/gbstock/quote/v1/period"
_TOKEN_PATH = "/oauth2/token"
_MAX_RESPONSE = 20_000_000
_MAX_PAGES = 1000
_TOKEN_LIFETIME = 86400
_CODE = re.compile(r"[A-Za-z0-9]{6}\Z", flags=re.ASCII)
_US_CODE = re.compile(r"[A-Z][A-Z0-9.\-]{0,14}\Z", flags=re.ASCII)
_NUMBER = re.compile(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?\Z", flags=re.ASCII)
_KR_SUMMARY_FIELDS = frozenset("dca nxt_dd_dca nxt2_dd_dca drn_pbl_amt mgg_rt nas_amt tot_aet_amt tot_byn_amt tot_eal_amt tot_eal_pls pft_rt rba int_ny_pmt_amt ny_rdp_amt ect_lga lon_amt sba_amt orr_pbl_amt2 orr_pbl_amt3 orr_pbl_amt4 slo_mgg_amt csh_wtm sba_wtm sll_edn_amt".split())
_US_SUMMARY_FIELDS = frozenset("abk_amt eal_amt_sum eal_pls_sum_amt krw_pft_rt krw_dca krw_ny_stl_xcl_amt tot_aet_amt fc_abk_amt fc_eal_amt fc_eal_pls_amt pft_rt fc_dca fc_ny_stl_xcl_amt fc_aet_amt ptps_ttn_amt ptps_ttn_amt1".split())


class ProviderError(RuntimeError):
    """A safe error message that contains no credentials or server payload."""


class _HTTPFailure(Exception):
    def __init__(self, status: int, headers: dict[str, str]):
        self.status = status
        self.headers = headers
        super().__init__(f"HTTP {status}")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener():
    return build_opener(HTTPSHandler(context=ssl.create_default_context()), _NoRedirect())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _header(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > 8192:
        raise ProviderError("NH 인증 또는 연속조회 값의 형식이 올바르지 않습니다.")
    if not value.isascii() or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ProviderError("NH 인증 또는 연속조회 값의 형식이 올바르지 않습니다.")
    return value


def _guard_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        if parsed.username or parsed.password or parsed.fragment or parsed.scheme != "https":
            raise ValueError
        api = parsed.hostname == "api.nhplug.com" and parsed.port == 8443 and parsed.path in {
            _TOKEN_PATH, _QUOTE_PATH, _TRADES_PATH, _ACCOUNTS_PATH, _BALANCE_PATH,
            _US_BALANCE_PATH, _US_QUOTE_PATH, _US_TRADES_PATH, _US_DAILY_PATH,
        }
        master = parsed.hostname == "www.nhplug.com" and parsed.port in (None, 443) and parsed.path == "/instruments/m_new_stock.mst" and not parsed.query
        if not (api or master) or (parsed.query and parsed.path != _TOKEN_PATH):
            raise ValueError
    except (TypeError, ValueError):
        raise ProviderError("허용되지 않은 NH 연결 주소입니다.") from None


def _response_bytes(opener, request: Request) -> tuple[bytes, dict[str, str]]:
    _guard_url(request.full_url)
    try:
        with opener.open(request, timeout=15) as response:
            if response.geturl() != request.full_url:
                raise ProviderError("NH 응답의 주소 변경을 차단했습니다.")
            headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
            if response.status != 200:
                raise _HTTPFailure(int(response.status), headers)
            raw = response.read(_MAX_RESPONSE + 1)
            if not isinstance(raw, bytes) or len(raw) > _MAX_RESPONSE:
                raise ProviderError("NH 응답의 크기 또는 형식이 올바르지 않습니다.")
            return raw, headers
    except HTTPError as error:
        status = int(error.code)
        headers = {str(key).lower(): str(value) for key, value in (error.headers or {}).items()}
        error.close()
        raise _HTTPFailure(status, headers) from None
    except (URLError, OSError, ValueError):
        raise ProviderError("NH 연결에 실패했습니다. 네트워크와 연결 설정을 확인하세요.") from None


def _bad_json_constant(value):
    raise ValueError("Non-finite JSON number")


def _json_response(opener, request: Request) -> tuple[dict, dict[str, str]]:
    raw, headers = _response_bytes(opener, request)
    try:
        payload = json.loads(raw.decode("utf-8"), parse_float=Decimal, parse_constant=_bad_json_constant)
        if not isinstance(payload, dict):
            raise ValueError
    except (ValueError, TypeError):
        raise ProviderError("NH 응답을 해석할 수 없습니다.") from None
    return payload, headers


def _decimal(value: object, *, allow_zero: bool = False, allow_negative: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ProviderError("NH 응답의 수치가 올바르지 않습니다.")
    text = str(value).strip()
    if len(text) > 100 or not _NUMBER.fullmatch(text):
        raise ProviderError("NH 응답의 수치가 올바르지 않습니다.")
    try:
        number = Decimal(text.replace(",", ""))
    except InvalidOperation:
        raise ProviderError("NH 응답의 수치가 올바르지 않습니다.") from None
    if not number.is_finite() or (number < 0 and not allow_negative) or (number == 0 and not allow_zero):
        raise ProviderError("NH 응답에 유효한 양수 수치가 없습니다.")
    return number


def _decimal_text(number: Decimal) -> str:
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _instrument_code(value: object) -> str:
    if not isinstance(value, str) or not _CODE.fullmatch(value):
        raise ProviderError("NH 종목코드의 형식이 올바르지 않습니다.")
    return value.upper()


def _us_code(value: object) -> str:
    if not isinstance(value, str) or not _US_CODE.fullmatch(value.upper()):
        raise ProviderError("NH 미국 종목코드의 형식이 올바르지 않습니다.")
    return value.upper()


def _account_number(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9-]+", value):
        raise ProviderError("NH 계좌번호 형식이 올바르지 않습니다.")
    account = value.replace("-", "")
    if not 8 <= len(account) <= 14:
        raise ProviderError("NH 계좌번호 형식이 올바르지 않습니다.")
    return account


def parse_nh_master(raw: bytes) -> list[dict]:
    """Parse 236 data bytes plus LF per CP949 master record, atomically.

    A CRLF line ending or missing final newline is harmless. Empty records,
    duplicate codes, bad decoding, and any wrong width reject the entire file.
    """
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_RESPONSE:
        raise ProviderError("NH 종목 마스터가 비어 있거나 올바르지 않습니다.")
    records = raw.split(b"\n")
    if records[-1] == b"":
        records.pop()
    updated_at = _now_iso()
    result: list[dict] = []
    seen: set[str] = set()
    try:
        for record in records:
            if record.endswith(b"\r"):
                record = record[:-1]
            if len(record) != 236:
                raise ValueError
            decoded = record.decode("cp949", errors="strict")
            if not decoded.isprintable():
                raise ValueError
            code = _instrument_code(record[0:6].decode("ascii"))
            market = record[6:7].decode("ascii")
            if not re.fullmatch(r"[0-9A-Z]", market) or code in seen:
                raise ValueError
            name = record[7:48].decode("cp949", errors="strict").strip().lstrip("*").strip()
            if not name:
                raise ValueError
            venture = record[165:166].decode("ascii")
            sector = record[199:205].decode("ascii").strip()
            if not re.fullmatch(r"[0-9A-Z ]", venture) or not re.fullmatch(r"[0-9A-Z]*", sector):
                raise ValueError
            asset_type = "ETN" if market == "A" else "ETF" if market == "1" and venture == "8" else "주식"
            result.append({"id": f"KR:{code}", "market": "KR", "code": code, "name": name,
                           "currency": "KRW", "asset_type": asset_type, "sector": sector,
                           "updated_at": updated_at, "deleted": False})
            seen.add(code)
    except (ValueError, UnicodeError, ProviderError):
        raise ProviderError("NH 종목 마스터 형식이 변경되었거나 파일이 손상되었습니다. 가져오기를 중단했습니다.") from None
    return result


def load_nh_master() -> list[dict]:
    request = Request(MASTER_URL, method="GET", headers={"Accept": "application/octet-stream", "User-Agent": "StockJournal/1"})
    try:
        raw, _ = _response_bytes(_opener(), request)
    except _HTTPFailure as error:
        raise ProviderError(f"NH 종목 마스터 다운로드에 실패했습니다 (HTTP {error.status}).") from None
    return parse_nh_master(raw)


def _business_check(payload: dict) -> None:
    if payload.get("error") or payload.get("error_code") not in (None, "", 0, "0"):
        raise ProviderError("NH 서버에서 요청을 처리하지 못했습니다. 인증과 조회 조건을 확인하세요.")
    if re.search(r"오류|실패|인증|권한|유효하지|잘못|초과|거부|만료|불가능|오입력|입력하세요", _status_message(payload)):
        raise ProviderError("NH 서버에서 요청을 처리하지 못했습니다. 인증과 조회 조건을 확인하세요.")


def _status_message(payload: dict) -> str:
    messages = [payload.get("rsp_msg", "")]
    nested = payload.get("message")
    if nested is not None:
        if not isinstance(nested, dict):
            raise ProviderError("NH 응답의 상태 정보가 올바르지 않습니다.")
        messages.extend([nested.get("usr_msg", ""), nested.get("msg", "")])
    if any(not isinstance(message, str) for message in messages):
        raise ProviderError("NH 응답의 상태 정보가 올바르지 않습니다.")
    return " ".join(messages)


def _explicit_no_data(payload: dict) -> bool:
    message = re.sub(r"\s+", "", _status_message(payload)).lower()
    return (("조회" in message and ("없" in message or "0건" in message))
            or "데이터가없" in message or "거래내역이없" in message
            or "체결내역이없" in message or "nodata" in message or "norecords" in message)


def _retry_delay(headers: dict[str, str], attempt: int) -> float:
    try:
        delay = float(headers.get("retry-after", ""))
        if not math.isfinite(delay):
            raise ValueError
    except (TypeError, ValueError):
        delay = 0.25 * (2 ** attempt)
    return max(0.05, min(2.0, delay))


def _http_message(status: int) -> str:
    if status == 401:
        return "NH 인증에 실패했습니다. 앱 키와 이용 권한을 확인하세요."
    if status == 429:
        return "NH 호출 한도를 초과했습니다. 잠시 후 다시 시도하세요."
    return f"NH 서버 요청에 실패했습니다 (HTTP {status})."


class NHProvider:
    """Read-only live NH discovery, holdings, prices and executed cash orders.

    Account discovery returns account numbers only to the caller's memory.
    Balance snapshots never create journal trades or infer opening balances.
    Overseas market support is deliberately limited to US stocks in USD.
    """

    def __init__(self, app_key: str, app_secret: str, cache_dir: str | Path):
        self._app_key = _header(app_key)
        self._app_secret = _header(app_secret)
        self._opener = _opener()
        digest = hashlib.sha256(json.dumps([app_key, app_secret], separators=(",", ":")).encode()).hexdigest()[:24]
        self._secrets = SecretStore(Path(cache_dir) / f"nh-token-{digest}.bin")
        self._token = ""
        self._expires_at = 0.0
        self._last_request_at: float | None = None
        cached = self._secrets.get("token")
        if cached:
            try:
                parsed = json.loads(cached)
                issued = float(parsed["issued_at"])
                expires = float(parsed["expires_at"])
                cached_token = _header(parsed["access_token"])
                if math.isfinite(issued) and math.isfinite(expires) and 0 < expires - issued <= _TOKEN_LIFETIME and issued <= time.time():
                    self._token, self._expires_at = cached_token, expires
            except (TypeError, ValueError, KeyError, ProviderError):
                pass  # An unusable protected cache can be replaced by a fresh token.

    def _get_token(self, *, force: bool = False) -> str:
        if not force and self._token and time.time() < self._expires_at - 60:
            return self._token
        if force:
            self._token, self._expires_at = "", 0.0
            self._secrets.set("token", "")
        query = urlencode({"appkey": self._app_key, "appsecretkey": self._app_secret,
                           "grant_type": "client_credentials", "scope": "oob"})
        request = Request(API_URL + _TOKEN_PATH + "?" + query, data=b"", method="POST",
                          headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        for attempt in range(3):
            try:
                payload, _ = _json_response(self._opener, request)
                break
            except _HTTPFailure as error:
                if error.status == 429 and attempt < 2:
                    time.sleep(_retry_delay(error.headers, attempt))
                    continue
                raise ProviderError(_http_message(error.status)) from None
        _business_check(payload)
        new_token = _header(payload.get("access_token"))
        try:
            lifetime = float(payload.get("expires_in", _TOKEN_LIFETIME))
            if not math.isfinite(lifetime) or lifetime <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ProviderError("NH 인증 토큰의 유효기간이 올바르지 않습니다.") from None
        issued_at = time.time()
        expires_at = issued_at + min(lifetime, _TOKEN_LIFETIME)
        self._secrets.set("token", json.dumps({"access_token": new_token, "issued_at": issued_at,
                                              "expires_at": expires_at}, separators=(",", ":")))
        self._token, self._expires_at = new_token, expires_at
        return new_token

    def _post(self, path: str, fields: dict, *, cts: str = "", cts_flag: str = "") -> tuple[dict, dict[str, str]]:
        token = self._get_token()
        refreshed = False
        rate_retries = 0
        while True:
            headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json; charset=UTF-8",
                       "Accept": "application/json"}
            if cts:
                headers["cts"] = _header(cts)
            if cts_flag:
                headers["cts_flag"] = _header(cts_flag)
            request = Request(API_URL + path, data=json.dumps({"Input_0": fields}, ensure_ascii=False).encode("utf-8"),
                              headers=headers, method="POST")
            now = time.monotonic()
            if self._last_request_at is not None and now - self._last_request_at < 0.25:
                time.sleep(max(0.0, 0.25 - (now - self._last_request_at)))
            self._last_request_at = time.monotonic()
            try:
                payload, response_headers = _json_response(self._opener, request)
            except _HTTPFailure as error:
                if error.status == 401 and not refreshed:
                    token = self._get_token(force=True)
                    refreshed = True
                    continue
                if error.status == 429 and rate_retries < 2:
                    time.sleep(_retry_delay(error.headers, rate_retries))
                    rate_retries += 1
                    continue
                raise ProviderError(_http_message(error.status)) from None
            _business_check(payload)
            return payload, response_headers

    def _pages(self, path: str, fields: dict):
        cts = cts_flag = ""
        seen: set[str] = set()
        for page in range(_MAX_PAGES):
            payload, headers = self._post(path, fields, cts=cts, cts_flag=cts_flag)
            next_cts, next_flag = _continuation(payload, headers)
            if next_cts and (next_cts in seen or page + 1 >= _MAX_PAGES):
                raise ProviderError("NH 연속조회가 끝나지 않았습니다. 불완전한 내역의 가져오기를 중단했습니다.")
            yield payload
            if not next_cts:
                return
            seen.add(next_cts)
            cts, cts_flag = next_cts, next_flag

    def discover_accounts(self) -> list[dict]:
        """Return runtime-only account numbers and explicit environment eligibility."""
        accounts = {}
        for payload in self._pages(_ACCOUNTS_PATH, {}):
            for row in _output_rows(payload, "Output_0", "계좌 목록"):
                account = _account_number(row.get("acct_no"))
                account_type = row.get("acct_type")
                if len(account) != 11 or not isinstance(account_type, str) or not re.fullmatch(r"[0-9]{2}", account_type):
                    raise ProviderError("NH 계좌 목록의 형식이 올바르지 않습니다.")
                environment = {"01": "live", "02": "live", "03": "mock"}.get(account_type, "unknown")
                result = {"account_number": account, "account_type": account_type, "environment": environment,
                          "usable": environment == "live", "masked_number": "*" * (len(account) - 4) + account[-4:]}
                if account in accounts and accounts[account] != result:
                    raise ProviderError("NH 같은 계좌의 환경 구분이 서로 다릅니다. 연결을 중단했습니다.")
                accounts[account] = result
        return list(accounts.values())

    def get_holdings(self, account_number: str, *, overseas: bool = False,
                     country_code: str = "200", currency: str = "USD") -> dict:
        """Return a complete broker balance snapshot, separate from journal history."""
        account = _account_number(account_number)
        if overseas and (country_code != "200" or currency != "USD"):
            raise ProviderError("자동 해외 잔고 조회는 현재 미국(200)·USD 계좌만 지원합니다.")
        if overseas:
            path, summary_fields = _US_BALANCE_PATH, _US_SUMMARY_FIELDS
            fields = {"act_no": account, "qut_iqr_dit_cd": "9", "fc_sec_trd_nat_cd": country_code,
                      "cur_cd": currency, "xns_dit_cd": "1"}
        else:
            path, summary_fields = _BALANCE_PATH, _KR_SUMMARY_FIELDS
            fields = {"act_no": account, "bnc_bse_cd": "5", "ltg_aot_dit_cd": "9", "aet_bse": "2",
                      "qut_dit_cd": "UNT", "aly_qut_cd": "1"}
        as_of = _now_iso()
        holdings, summary = {}, {}
        for payload in self._pages(path, fields):
            output = payload.get("Output_0")
            if output is not None:
                if not isinstance(output, dict):
                    raise ProviderError("NH 잔고 요약의 형식이 올바르지 않습니다.")
                summary = {key: _decimal_text(_decimal(value, allow_zero=True, allow_negative=True))
                           for key, value in output.items() if key in summary_fields and value not in (None, "")}
            else:
                # Domestic totals are only authoritative after the final page.
                summary = {}
            for raw in _output_rows(payload, "Output_1", "보유 종목", allow_summary=bool(summary)):
                row = _holding(raw, overseas=overseas, as_of=as_of)
                if row is None:
                    continue
                key = (row["instrument_id"], row["position_type"], row["loan_date"], row["maturity_date"])
                if key in holdings and holdings[key] != row:
                    raise ProviderError("NH 같은 보유 종목의 잔고가 조회 중 달라졌습니다. 다시 조회하세요.")
                holdings[key] = row
        warnings = ["NH 잔고 스냅샷이며 매매 이력을 대체하지 않습니다. 기초 보유나 거래를 자동 생성하지 않습니다."]
        if not summary:
            warnings.append("마지막 조회 응답에 계좌 요약이 없어 합계 금액을 확정하지 않았습니다.")
        return {"holdings": list(holdings.values()), "summary": summary, "source": "NH balance",
                "as_of": as_of, "market": "US" if overseas else "KR", "currency": "USD" if overseas else "KRW",
                "warnings": warnings}

    def quotes(self, instruments) -> dict[str, dict[str, str]]:
        targets: dict[str, tuple[str, str]] = {}
        for instrument in instruments:
            if not isinstance(instrument, dict):
                raise ProviderError("시세 조회 종목의 형식이 올바르지 않습니다.")
            market = instrument.get("market")
            if market not in {"KR", "US"} or instrument.get("deleted"):
                continue
            identifier = instrument.get("id")
            code = _instrument_code(instrument.get("code")) if market == "KR" else _us_code(instrument.get("code"))
            if market == "US" and instrument.get("currency", "USD") != "USD":
                raise ProviderError("미국 종목의 통화는 USD여야 합니다.")
            if not isinstance(identifier, str) or not identifier or (identifier in targets and targets[identifier] != (market, code)):
                raise ProviderError("시세 조회 종목의 식별자가 올바르지 않습니다.")
            targets[identifier] = (market, code)
        result = {}
        for identifier, (market, code) in targets.items():
            path = _QUOTE_PATH if market == "KR" else _US_QUOTE_PATH
            fields = {"iem_cd": code, "market_cd": "UNT"} if market == "KR" else {"iem_cd": code}
            payload, _ = self._post(path, fields)
            output = payload.get("Output_0")
            if not isinstance(output, dict):
                raise ProviderError("NH 응답에 현재가 정보가 없습니다.")
            if market == "US" and (output.get("iem_cd") != code or output.get("currency_unit") != "USD"):
                raise ProviderError("NH 해외 현재가의 종목 또는 통화가 조회 조건과 다릅니다.")
            price = _decimal(output.get("stck_prpr" if market == "KR" else "trdprc"))
            result[identifier] = {"price": _decimal_text(price), "as_of": _now_iso(), "source": "NH polling"}
            if market == "US":
                result[identifier]["currency"] = "USD"
        return result

    def overseas_daily_prices(self, symbol: str, enddate: str | date | None = None, count: int = 260) -> dict:
        """US regular-session daily bars; split/dividend adjustment is unverified.

        Today's bars are eligible ten minutes after the published regular close.
        The bounded calendar includes scheduled holidays and early closes.
        """
        symbol = _us_code(symbol)
        if isinstance(count, bool) or not isinstance(count, int) or not 201 <= count <= 9999:
            raise ProviderError("NH 일봉 조회 건수는 201~9999 사이의 정수여야 합니다.")
        try:
            from .market_calendar import SOURCE as calendar_source, latest_completed
            cutoff = _date(latest_completed(datetime.now(timezone.utc)))
        except ZoneInfoNotFoundError:
            raise ProviderError("뉴욕 시간대 자료가 없습니다. tzdata 설치 또는 앱 업데이트가 필요합니다.") from None
        except ValueError:
            raise ProviderError("미국 거래 달력의 지원 범위를 벗어나 완료 일봉을 확인할 수 없습니다.") from None
        end = min(_date(enddate), cutoff) if enddate is not None else cutoff
        rows = {}
        for payload in self._pages(_US_DAILY_PATH, {"iem_cd": symbol, "end_dt": end.strftime("%Y%m%d"),
                "count": f"{count:04d}", "maxavg": "200", "gubun": "3", "xtick": "0001", "today_cls": "0", "market_cls": "1"}):
            metadata = payload.get("Output_0")
            if metadata is not None:
                if not isinstance(metadata, list) or any(not isinstance(item, dict) for item in metadata):
                    raise ProviderError("NH 일봉 요약의 형식이 올바르지 않습니다.")
                if any(item.get("iem_cd", symbol) != symbol for item in metadata):
                    raise ProviderError("NH 일봉 응답의 종목이 조회 조건과 다릅니다.")
            for raw in _output_rows(payload, "Output_1", "일봉"):
                day = _compact_date(raw.get("trade_date"))
                if day > end or day.weekday() >= 5 or day in rows:
                    raise ProviderError("NH 일봉에 중복·미완료 날짜 또는 정규 거래일이 아닌 날짜가 있습니다.")
                values = {key: _decimal(raw.get(field)) for key, field in
                          (("open", "open_prc"), ("high", "high"), ("low", "low"), ("close", "close_prc"))}
                if not values["low"] <= min(values["open"], values["close"]) <= max(values["open"], values["close"]) <= values["high"]:
                    raise ProviderError("NH 일봉의 시가·고가·저가·종가 관계가 올바르지 않습니다.")
                volume = _decimal(raw.get("movolume"), allow_zero=True)
                if volume != volume.to_integral_value():
                    raise ProviderError("NH 일봉의 거래량이 정수가 아닙니다.")
                rows[day] = {"date": day.isoformat(), **{key: _decimal_text(value) for key, value in values.items()},
                             "volume": _decimal_text(volume)}
        if len(rows) > count:
            raise ProviderError("NH 일봉 응답이 요청 건수를 초과했습니다.")
        warnings = ["NH 명세에 분할·배당 수정주가 여부가 명시되지 않아 adjustment=unknown입니다. 수정 기준 확인 전 전략 신호에 사용하지 마세요.",
                    "정규장만 조회하며 공개 거래 달력의 종가 시각 10분 후를 완료 기준으로 사용합니다. 긴급 휴장·시세 갱신은 별도 확인해야 합니다."]
        if len(rows) < 201:
            warnings.append("일봉이 201개 미만입니다. 200일선과 전일 비교에 필요한 이력이 부족합니다.")
        return {"rows": [rows[day] for day in sorted(rows)], "source": "NH daily polling", "symbol": symbol,
                "currency": "USD", "session": "regular", "adjustment": "unknown", "finalization": "completed_sessions",
                "finalization_policy": "published_calendar_close_plus_10m", "cutoff_time": "regular close + 10 minutes America/New_York",
                "calendar_source": calendar_source,
                "as_of": _now_iso(), "end_date": end.isoformat(), "warnings": warnings}

    def trades(self, account_number: str, startdate: str | date, enddate: str | date) -> list[dict]:
        return self._order_history(account_number, startdate, enddate, overseas=False)

    def overseas_trades(self, account_number: str, startdate: str | date, enddate: str | date) -> list[dict]:
        """Import cumulative US cash executions for review; fees/taxes remain unknown."""
        return self._order_history(account_number, startdate, enddate, overseas=True)

    def _order_history(self, account_number: str, startdate: str | date, enddate: str | date, *, overseas: bool) -> list[dict]:
        account = _account_number(account_number)
        start, end = _date(startdate), _date(enddate)
        if end < start or (end - start).days + 1 > 367:
            raise ProviderError("NH 조회 기간은 순서가 올바른 최대 367일이어야 합니다.")
        account_hash = hashlib.sha256(account.encode("ascii")).hexdigest()[:24]
        result: dict[str, dict] = {}
        updated_at = _now_iso()
        day = start
        while day <= end:
            fields = {"orr_dt": day.strftime("%Y%m%d"), "act_no": account, "ost_cns_dit": "1"}
            fields.update({"oss_sby_dit_cd": "0", "sot_dit": "0"} if overseas else {"orr_mkt_cd": "00"})
            for payload in self._pages(_US_TRADES_PATH if overseas else _TRADES_PATH, fields):
                for source_row in _output_rows(payload, "Output_0", "체결 내역"):
                    row = (_overseas_trade if overseas else _trade)(source_row, account_hash, day, updated_at)
                    if row is None:
                        continue
                    previous = result.get(row["id"])
                    if previous:
                        if row["instrument_id"] != previous["instrument_id"] or row["side"] != previous["side"]:
                            raise ProviderError("NH 동일 주문의 종목 또는 매매 구분이 일치하지 않습니다. 가져오기를 중단했습니다.")
                        quantity, previous_quantity = Decimal(row["quantity"]), Decimal(previous["quantity"])
                        if quantity == previous_quantity and Decimal(row["price"]) != Decimal(previous["price"]):
                            raise ProviderError("NH 동일 누적체결 수량의 금액이 일치하지 않습니다. 가져오기를 중단했습니다.")
                        if quantity <= previous_quantity:
                            continue
                    result[row["id"]] = row
            day += timedelta(days=1)
        return sorted(result.values(), key=lambda row: (row["executed_at"], row["sequence"], row["id"]))


def _date(value: str | date) -> date:
    if type(value) is date:
        return value
    try:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise ProviderError("NH 조회 날짜는 YYYY-MM-DD 형식이어야 합니다.") from None


def _compact_date(value: object) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}", value):
        raise ProviderError("NH 응답 날짜의 형식이 올바르지 않습니다.")
    return _date(f"{value[:4]}-{value[4:6]}-{value[6:]}")


def _output_rows(payload: dict, block: str, label: str, *, allow_summary: bool = False) -> list[dict]:
    output = payload.get(block)
    empty_allowed = _explicit_no_data(payload) or allow_summary
    if output is None and empty_allowed:
        output = []
    if not isinstance(output, list) or (not output and not empty_allowed) or any(not isinstance(row, dict) for row in output):
        raise ProviderError(f"NH 응답에 {label} 또는 명시적인 조회 없음 결과가 없습니다.")
    return output


def _optional_decimal(source: dict, field: str, *, signed: bool = False) -> str | None:
    value = source.get(field)
    if value in (None, ""):
        return None
    return _decimal_text(_decimal(value, allow_zero=True, allow_negative=signed))


def _text_value(value: object, *, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str) or len(value) > 200 or (value and not value.isprintable()):
        raise ProviderError("NH 응답의 종목 정보가 올바르지 않습니다.")
    return value.strip() or default


def _holding(source: dict, *, overseas: bool, as_of: str) -> dict | None:
    quantity = _decimal(source.get("cns_bse_bnc_qty" if overseas else "itg_bnc_qty"), allow_zero=True)
    if quantity == 0:
        return None
    if overseas and (source.get("fc_sec_trd_nat_cd") != "200" or source.get("cur_cd") != "USD"):
        raise ProviderError("NH 미국 잔고 응답의 국가 또는 통화가 조회 조건과 다릅니다.")
    code = _us_code(source.get("iem_cd")) if overseas else _instrument_code(source.get("iem_cd"))
    market, currency = ("US", "USD") if overseas else ("KR", "KRW")
    loan, maturity = source.get("lon_dt" if overseas else "lon_byn_dt", ""), source.get("xrn_dt", "")
    loan = "" if loan in (None, "", "00000000") else _compact_date(loan).isoformat()
    maturity = "" if maturity in (None, "", "00000000") else _compact_date(maturity).isoformat()
    return {"instrument_id": f"{market}:{code}", "code": code, "name": _text_value(source.get("iem_nm"), default=code),
            "market": market, "currency": currency, "quantity": _decimal_text(quantity),
            "average_price": _optional_decimal(source, "fc_phs_uit_pr" if overseas else "phs_pr"),
            "current_price": _optional_decimal(source, "fc_sec_end_pr" if overseas else "now_pr"),
            "market_value": _optional_decimal(source, "fc_eal_amt" if overseas else "eal_amt"),
            "book_value": _optional_decimal(source, "fc_abk_amt") if overseas else None,
            "unrealized_pnl": _optional_decimal(source, "fc_eal_pls_amt" if overseas else "eal_pls_amt", signed=True),
            "position_type": _text_value(source.get("cfd_lon_cd_nm" if overseas else "tp_cd_nm")),
            "loan_date": loan, "maturity_date": maturity,
            "loan_balance": None if overseas else _optional_decimal(source, "lon_bnc_amt"),
            # Names alone do not establish a documented cash/credit code.
            "cash_position": False, "cash_position_status": "unverified", "source": "NH balance", "as_of": as_of}


def _order_number(value: object, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ProviderError("NH 주문번호 형식이 올바르지 않습니다.")
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,30}", text):
        raise ProviderError("NH 주문번호 형식이 올바르지 않습니다.")
    number = int(text)
    if not allow_zero and number == 0:
        raise ProviderError("NH 주문번호 형식이 올바르지 않습니다.")
    return number


def _trade(source: object, account_hash: str, day: date, updated_at: str) -> dict | None:
    if not isinstance(source, dict):
        raise ProviderError("NH 체결 내역의 행 형식이 올바르지 않습니다.")
    quantity = _decimal(source.get("tot_cns_qty"), allow_zero=True)
    if quantity == 0:
        return None
    if source.get("cfd_lon_cd") != "00":
        raise ProviderError("NH 신용·대출 구분이 일반 현금이 아닌 주문은 자동 가져오기를 지원하지 않습니다.")
    original_order = source.get("org_itg_orr_no", "0")
    if original_order in (None, ""):
        original_order = "0"
    if _order_number(original_order, allow_zero=True) > 0:
        raise ProviderError("NH 정정·취소 연결 주문은 중복 위험이 있어 자동 가져오기를 중단했습니다.")
    side = {"매수": "BUY", "현금매수": "BUY", "매도": "SELL", "현금매도": "SELL"}.get(source.get("sby_dit_cd_nm"))
    if not side:
        raise ProviderError("NH 응답의 매매 구분을 안전하게 해석할 수 없습니다.")
    order = _order_number(source.get("itg_orr_no"))
    code = _instrument_code(source.get("iem_cd"))
    warnings = ["NH 일별 주문 누적체결입니다. 개별 체결시각이 없어 주문번호 순서로 정렬합니다.",
                "NH 응답에 수수료와 세금이 없어 각각 0으로 가져왔습니다. 거래명세서와 대조해 수정하세요."]
    if "cns_amt" in source and source["cns_amt"] not in (None, ""):
        amount = _decimal(source["cns_amt"])
        with localcontext() as context:
            context.prec = 50
            price = amount / quantity
        if Fraction(price) * Fraction(quantity) != Fraction(amount):
            warnings.append("체결금액을 수량으로 나눈 단가를 유효숫자 50자리로 계산하여 극소수의 반올림 차이가 있을 수 있습니다.")
    else:
        price = _decimal(source.get("cns_avg_uit_pr"))
        warnings.append("체결금액이 없어 NH의 표시 평균단가를 사용했습니다. 표시 단가의 반올림 차이를 확인하세요.")
    name = source.get("iem_nm", code)
    if not isinstance(name, str):
        raise ProviderError("NH 종목명 형식이 올바르지 않습니다.")
    identifier = f"nh:{account_hash}:{day.isoformat()}:{order}"
    return {"id": identifier, "account_id": "", "instrument_id": f"KR:{code}", "executed_at": day.isoformat(),
            "side": side, "quantity": _decimal_text(quantity), "price": _decimal_text(price),
            "fee": "0", "tax": "0", "currency": "KRW", "sequence": order,
            "memo": "NH 일별 주문 누적체결 · 수수료/세금 확인 필요", "source": "NH", "external_id": identifier,
            "updated_at": updated_at, "deleted": False, "_instrument_name": name.strip() or code,
            "_instrument_market": "KR", "_instrument_code": code, "_instrument_currency": "KRW", "_warnings": warnings}


def _overseas_trade(source: dict, account_hash: str, day: date, updated_at: str) -> dict | None:
    quantity = _decimal(source.get("cns_qty"), allow_zero=True)
    if quantity == 0:
        return None
    if source.get("fc_sec_trd_nat_cd") != "200":
        raise ProviderError("자동 해외 체결 가져오기는 현재 미국 주식만 지원합니다. 다른 국가의 체결이 있어 중단했습니다.")
    if source.get("cfd_lon_cd") != "00":
        raise ProviderError("NH 해외 신용·대출 주문은 자동 가져오기를 지원하지 않습니다.")
    original = source.get("org_orr_no")
    original = "0" if original in (None, "") else original
    amended = [_decimal("0" if source.get(field) in (None, "") else source[field], allow_zero=True)
               for field in ("cor_qty", "can_qty")]
    if _order_number(original, allow_zero=True) > 0 or any(quantity > 0 for quantity in amended):
        raise ProviderError("NH 해외 정정·취소 연결 주문은 중복 위험이 있어 가져오기를 중단했습니다.")
    actual_day = _compact_date(source.get("orr_dt"))
    if actual_day != day:
        raise ProviderError("NH 해외 체결의 주문일자가 조회 날짜와 다릅니다. 가져오기를 중단했습니다.")
    side = {"1": "SELL", "2": "BUY"}.get(source.get("oss_sby_dit_cd"))
    if side is None:
        raise ProviderError("NH 해외 체결의 매매 구분을 안전하게 해석할 수 없습니다.")
    order, code = _order_number(source.get("orr_no")), _us_code(source.get("iem_cd"))
    price = _decimal(source.get("cns_pr"))
    identifier = f"nh-us:{account_hash}:{day.isoformat()}:{order}"
    warnings = ["NH 해외 주문별 누적체결입니다. 주문일자를 저장하며 개별 체결시각은 제공되지 않습니다.",
                "NH 체결가격을 사용했습니다. 원본 체결금액·수수료·세금이 없어 명세서 대조가 필요합니다.",
                "수수료와 세금을 각각 0으로 가져왔습니다. 확인 후 수정해야 정확한 손익이 계산됩니다."]
    return {"id": identifier, "account_id": "", "instrument_id": f"US:{code}", "executed_at": day.isoformat(),
            "side": side, "quantity": _decimal_text(quantity), "price": _decimal_text(price), "fee": "0", "tax": "0",
            "currency": "USD", "sequence": order, "memo": "NH 해외 주문 누적체결 · 수수료/세금 확인 필요",
            "source": "NH", "external_id": identifier, "updated_at": updated_at, "deleted": False,
            "_instrument_name": _text_value(source.get("iem_nm"), default=code), "_instrument_market": "US",
            "_instrument_code": code, "_instrument_currency": "USD", "_warnings": warnings}


def _continuation(payload: dict, headers: dict[str, str]) -> tuple[str, str]:
    cursor = headers.get("cts", payload.get("cts", ""))
    flag = headers.get("cts_flag", payload.get("cts_flag", ""))
    cursor = _header(cursor, allow_empty=True).strip()
    flag = _header(flag, allow_empty=True).strip().upper()
    if flag not in ("", "Y", "N"):
        raise ProviderError("NH 연속조회 상태를 해석할 수 없습니다.")
    if flag == "N":
        return "", ""
    nested = payload.get("message") or {}
    continued = flag == "Y" or any(str(code) in {"00165", "00218"} for code in
        (payload.get("rsp_cd", ""), nested.get("msg_code", ""), nested.get("code", "")))
    if continued:
        if not cursor:
            raise ProviderError("NH 연속조회 키가 누락되어 불완전한 가져오기를 중단했습니다.")
        return cursor, flag
    if cursor:
        raise ProviderError("NH 연속조회의 종료 여부가 불분명하여 가져오기를 중단했습니다.")
    return "", ""
