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

from .secrets import SecretStore


MASTER_URL = "https://www.nhplug.com/instruments/m_new_stock.mst"
API_URL = "https://api.nhplug.com:8443"
_QUOTE_PATH = "/krstock/quote/v1/currentPrice"
_TRADES_PATH = "/krstock/inquiry/v1/dailyOrderExecution"
_TOKEN_PATH = "/oauth2/token"
_MAX_RESPONSE = 20_000_000
_MAX_PAGES = 1000
_TOKEN_LIFETIME = 86400
_CODE = re.compile(r"[A-Za-z0-9]{6}\Z", flags=re.ASCII)
_NUMBER = re.compile(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?\Z", flags=re.ASCII)


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
            _TOKEN_PATH, _QUOTE_PATH, _TRADES_PATH,
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


def _decimal(value: object, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ProviderError("NH 응답의 수치가 올바르지 않습니다.")
    text = str(value).strip()
    if len(text) > 100 or not _NUMBER.fullmatch(text):
        raise ProviderError("NH 응답의 수치가 올바르지 않습니다.")
    try:
        number = Decimal(text.replace(",", ""))
    except InvalidOperation:
        raise ProviderError("NH 응답의 수치가 올바르지 않습니다.") from None
    if not number.is_finite() or number < 0 or (number == 0 and not allow_zero):
        raise ProviderError("NH 응답에 유효한 양수 수치가 없습니다.")
    return number


def _decimal_text(number: Decimal) -> str:
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _instrument_code(value: object) -> str:
    if not isinstance(value, str) or not _CODE.fullmatch(value):
        raise ProviderError("NH 종목코드의 형식이 올바르지 않습니다.")
    return value.upper()


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
    message = payload.get("rsp_msg", "")
    if not isinstance(message, str):
        raise ProviderError("NH 응답의 상태 정보가 올바르지 않습니다.")
    if re.search(r"오류|실패|인증|권한|유효하지|잘못|초과|거부|만료|불가능|오입력", message):
        raise ProviderError("NH 서버에서 요청을 처리하지 못했습니다. 인증과 조회 조건을 확인하세요.")


def _explicit_no_data(payload: dict) -> bool:
    message = re.sub(r"\s+", "", str(payload.get("rsp_msg", ""))).lower()
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
    """NH domestic prices and cumulative executed cash orders; no order APIs."""

    def __init__(self, app_key: str, app_secret: str, cache_dir: str | Path):
        self._app_key = _header(app_key)
        self._app_secret = _header(app_secret)
        self._opener = _opener()
        digest = hashlib.sha256(json.dumps([app_key, app_secret], separators=(",", ":")).encode()).hexdigest()[:24]
        self._secrets = SecretStore(Path(cache_dir) / f"nh-token-{digest}.bin")
        self._token = ""
        self._expires_at = 0.0
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
            headers = {"Authorization": "Bearer " + token, "x-client-id": self._app_key,
                       "x-client-secret": self._app_secret, "Content-Type": "application/json; charset=UTF-8",
                       "Accept": "application/json"}
            if cts:
                headers["cts"] = _header(cts)
            if cts_flag:
                headers["cts_flag"] = _header(cts_flag)
            request = Request(API_URL + path, data=json.dumps({"Input_0": fields}, ensure_ascii=False).encode("utf-8"),
                              headers=headers, method="POST")
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

    def quotes(self, instruments) -> dict[str, dict[str, str]]:
        targets: dict[str, str] = {}
        for instrument in instruments:
            if not isinstance(instrument, dict):
                raise ProviderError("시세 조회 종목의 형식이 올바르지 않습니다.")
            if instrument.get("market") != "KR" or instrument.get("deleted"):
                continue
            identifier = instrument.get("id")
            code = _instrument_code(instrument.get("code"))
            if not isinstance(identifier, str) or not identifier or (identifier in targets and targets[identifier] != code):
                raise ProviderError("시세 조회 종목의 식별자가 올바르지 않습니다.")
            targets[identifier] = code
        result = {}
        for identifier, code in targets.items():
            payload, _ = self._post(_QUOTE_PATH, {"iem_cd": code, "market_cd": "UNT"})
            output = payload.get("Output_0")
            if not isinstance(output, dict):
                raise ProviderError("NH 응답에 현재가 정보가 없습니다.")
            price = _decimal(output.get("stck_prpr"))
            result[identifier] = {"price": _decimal_text(price), "as_of": _now_iso(), "source": "NH polling"}
        return result

    def trades(self, account_number: str, startdate: str | date, enddate: str | date) -> list[dict]:
        if not isinstance(account_number, str) or not re.fullmatch(r"[0-9-]+", account_number):
            raise ProviderError("NH 계좌번호 형식이 올바르지 않습니다.")
        account = account_number.replace("-", "")
        if not 8 <= len(account) <= 14:
            raise ProviderError("NH 계좌번호 형식이 올바르지 않습니다.")
        start, end = _date(startdate), _date(enddate)
        if end < start or (end - start).days + 1 > 367:
            raise ProviderError("NH 조회 기간은 순서가 올바른 최대 367일이어야 합니다.")
        account_hash = hashlib.sha256(account.encode("ascii")).hexdigest()[:24]
        result: dict[str, dict] = {}
        updated_at = _now_iso()
        day = start
        while day <= end:
            cts = cts_flag = ""
            seen: set[str] = set()
            for page in range(_MAX_PAGES):
                payload, headers = self._post(_TRADES_PATH, {"orr_dt": day.strftime("%Y%m%d"), "act_no": account,
                                                           "orr_mkt_cd": "00", "ost_cns_dit": "1"},
                                              cts=cts, cts_flag=cts_flag)
                output = payload.get("Output_0")
                if output is None and _explicit_no_data(payload):
                    output = []
                if not isinstance(output, list) or (not output and not _explicit_no_data(payload)):
                    raise ProviderError("NH 응답에 체결 내역 또는 명시적인 조회 없음 결과가 없습니다.")
                for source_row in output:
                    row = _trade(source_row, account_hash, day, updated_at)
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
                next_cts, next_flag = _continuation(payload, headers)
                if not next_cts:
                    break
                if next_cts in seen or page + 1 >= _MAX_PAGES:
                    raise ProviderError("NH 연속조회가 끝나지 않았습니다. 불완전한 내역의 가져오기를 중단했습니다.")
                seen.add(next_cts)
                cts, cts_flag = next_cts, next_flag
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
            "updated_at": updated_at, "deleted": False, "_instrument_name": name.strip() or code, "_warnings": warnings}


def _continuation(payload: dict, headers: dict[str, str]) -> tuple[str, str]:
    cursor = headers.get("cts", payload.get("cts", ""))
    flag = headers.get("cts_flag", payload.get("cts_flag", ""))
    cursor = _header(cursor, allow_empty=True).strip()
    flag = _header(flag, allow_empty=True).strip().upper()
    if flag not in ("", "Y", "N"):
        raise ProviderError("NH 연속조회 상태를 해석할 수 없습니다.")
    if flag == "N":
        return "", ""
    continued = flag == "Y" or str(payload.get("rsp_cd", "")) in {"00165", "00218"}
    if continued:
        if not cursor:
            raise ProviderError("NH 연속조회 키가 누락되어 불완전한 가져오기를 중단했습니다.")
        return cursor, flag
    if cursor:
        raise ProviderError("NH 연속조회의 종료 여부가 불분명하여 가져오기를 중단했습니다.")
    return "", ""
