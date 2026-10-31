"""Explicitly armed NH US cash LIMIT execution, separate from read-only providers.

Contract: https://www.nhplug.com/openapi-docs/gbstock/openapi.json (261015).
No network activity occurs at import. Writes are transmitted once, without any
HTTP/authentication retry. The automation journal owns durable intent deduplication.
An order handle is local reconciliation context, not a fictitious NH client ID.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from .automation import BrokerSnapshot, OrderRequest, OrderUpdate, Quote
from .market_calendar import NY, session as market_session
from .providers import (NHProvider, ProviderError, _account_number, _bad_json_constant,
                        _business_check, _compact_date, _continuation, _decimal,
                        _decimal_text, _header, _output_rows, _retry_delay, _us_code)


_HOSTS = {"live": "https://api.nhplug.com:8443", "mock": "https://moapi.nhplug.com:8443"}
_QUOTE = "/gbstock/quote/v1/current"
_BALANCE = "/gbstock/inquiry/v1/balance"
_AVAILABLE = "/gbstock/inquiry/v1/buyableAmount"
_ORDERS = "/gbstock/inquiry/v1/unexecuted"
_BUY = "/gbstock/order/v1/buy"
_SELL = "/gbstock/order/v1/sell"
_CANCEL = "/gbstock/order/v1/cancel"
_READ_PATHS = frozenset((_QUOTE, _BALANCE, _AVAILABLE, _ORDERS))
_WRITE_PATHS = frozenset((_BUY, _SELL, _CANCEL))
_KST = timezone(timedelta(hours=9))
_ZERO = Decimal(0)
_HANDLE_PREFIX = "nh-order-v1:"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        raise ProviderError("NH 주문 시각의 형식이 올바르지 않습니다.") from None


def _integer(value: object, *, zero: bool = False) -> int:
    number = _decimal(value, allow_zero=zero)
    if number != number.to_integral_value() or number > 999999999999999999:
        raise ProviderError("NH 주문 수량 또는 번호는 허용 범위의 정수여야 합니다.")
    return int(number)


def _price(value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise ProviderError("NH 지정가는 정확한 Decimal 금액이어야 합니다.")
    value = _decimal(value)
    with localcontext() as context:
        context.prec = 50
        if value > Decimal("999999999.99") or value != value.quantize(Decimal("0.01")):
            raise ProviderError("NH 지정가는 양수이며 소수점 두 자리 이하여야 합니다.")
    return value


def _candidate_dates(value: datetime) -> list[str]:
    # NH's ACK has no order date and the status schema does not specify its zone.
    return sorted({value.astimezone(NY).date().isoformat(), value.astimezone(_KST).date().isoformat()})


def _encode_handle(context: dict) -> str:
    raw = json.dumps(context, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    return _HANDLE_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_order_handle(handle: str) -> dict:
    """Read non-secret request identity and candidate dates for exact fill matching.

    A handle is not authenticated broker evidence; status reconciliation must
    still find one exact account/symbol/side/quantity/limit match at NH.
    """
    try:
        if not isinstance(handle, str) or not handle.startswith(_HANDLE_PREFIX) or len(handle) > 3000:
            raise ValueError
        encoded = handle[len(_HANDLE_PREFIX):]
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        context = json.loads(raw)
        keys = {"account_id", "environment", "order_number", "submitted_at", "symbol", "side", "quantity", "limit_price", "session"}
        if not isinstance(context, dict) or set(context) != keys:
            raise ValueError
        if not isinstance(context["account_id"], str) or not re.fullmatch(r"[a-f0-9]{64}", context["account_id"]):
            raise ValueError
        if context["environment"] not in _HOSTS or context["session"] not in {"regular", "afterhours"}:
            raise ValueError
        if context["side"] not in {"BUY", "SELL"} or type(context["quantity"]) is not int:
            raise ValueError
        _integer(context["quantity"])
        if not isinstance(context["order_number"], str) or not re.fullmatch(r"[0-9]{1,10}", context["order_number"]):
            raise ValueError
        _integer(context["order_number"])
        if _us_code(context["symbol"]) != context["symbol"] or not isinstance(context["limit_price"], str):
            raise ValueError
        _price(_decimal(context["limit_price"]))
        submitted = _utc(context["submitted_at"])
        return {**context, "candidate_dates": _candidate_dates(submitted)}
    except (ValueError, TypeError, KeyError, UnicodeError, ProviderError):
        raise ProviderError("NH 주문 식별자를 확인할 수 없습니다.") from None


def _json_exact(value) -> str:
    """Serialize this fixed request vocabulary without converting Decimal to float."""
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ProviderError("NH 요청 필드의 형식이 올바르지 않습니다.")
        return "{" + ",".join(json.dumps(key) + ":" + _json_exact(item) for key, item in value.items()) + "}"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if type(value) is int:
        return str(value)
    if isinstance(value, Decimal) and value.is_finite():
        return format(value, "f")
    raise ProviderError("NH 요청 수치의 형식이 올바르지 않습니다.")


class _HTTPFailure(Exception):
    def __init__(self, status, headers):
        self.status, self.headers = status, headers


class NHExecutionAdapter:
    """Live/mock adapter disabled until all explicit local guard flags are true.

    ``discovered_account`` is the runtime-only result of discover_accounts().
    ``account_id`` is SHA256 of the normalized account number, never the number.
    Session bounds come from the published calendar; supplied bounds may narrow
    them. Fresh NH market-period evidence is required in addition to the calendar.
    """

    def __init__(self, provider: NHProvider, discovered_account: dict, *, environment: str,
                 enabled: bool = False, session: str = "regular", session_verified: bool = False,
                 cash_account_verified: bool = False, session_open_at: datetime | None = None,
                 session_close_at: datetime | None = None):
        if not isinstance(provider, NHProvider) or not isinstance(discovered_account, dict):
            raise ProviderError("NH 제공자와 조회로 확인한 계좌가 필요합니다.")
        if environment not in _HOSTS or session not in {"regular", "afterhours"}:
            raise ProviderError("NH 주문 환경 또는 거래 세션을 확인하세요.")
        if any(type(value) is not bool for value in (enabled, session_verified, cash_account_verified)):
            raise ProviderError("NH 주문 사용 확인 값의 형식이 올바르지 않습니다.")
        number = _account_number(discovered_account.get("account_number"))
        kind = discovered_account.get("account_type")
        if len(number) != 11 or kind not in ({"01", "02"} if environment == "live" else {"03"}):
            raise ProviderError("NH 조회 계좌의 구분과 운영·모의투자 환경이 일치하지 않습니다.")
        if discovered_account.get("environment", environment) != environment:
            raise ProviderError("NH 조회 계좌의 환경이 선택한 환경과 다릅니다.")
        self.account_id = hashlib.sha256(number.encode("ascii")).hexdigest()
        self.environment, self.session = environment, session
        self._provider, self._account = provider, number
        self._enabled, self._session_verified, self._cash_verified = enabled, session_verified, cash_account_verified
        self._open_limit = None if session_open_at is None else _utc(session_open_at)
        self._close_limit = None if session_close_at is None else _utc(session_close_at)
        if self._open_limit and self._close_limit and self._open_limit >= self._close_limit:
            raise ProviderError("NH 주문 세션 시작·종료 시각을 확인하세요.")
        self._lock = threading.Lock()
        self._intents: dict[str, tuple[tuple, OrderUpdate]] = {}
        self._cancels: dict[str, OrderUpdate] = {}
        self._last_request_at: float | None = None
        self._stop_event: threading.Event | None = None

    def set_stop_event(self, event: threading.Event) -> None:
        """Bind the runtime stop flag, checked again at the write boundary."""
        if not isinstance(event, threading.Event):
            raise ProviderError("NH 실행 중지 신호의 형식이 올바르지 않습니다.")
        self._stop_event = event

    def _account_check(self, account_id: str) -> None:
        if account_id != self.account_id:
            raise ProviderError("주문 대상 계좌가 NH 연결 계좌와 다릅니다.")

    def _window(self) -> tuple[bool, datetime | None]:
        now = _utc(_now())
        try:
            info = market_session(now.astimezone(NY).date())
        except ValueError:
            return False, None
        if info is None:
            return False, None
        opened = _utc(info["open"] if self.session == "regular" else info["close"])
        closed = _utc(info["close"] if self.session == "regular" else info["after_close"])
        if self._open_limit is not None:
            opened = max(opened, self._open_limit)
        if self._close_limit is not None:
            closed = min(closed, self._close_limit)
        return self._session_verified and opened <= now < closed, closed

    def _write_guard(self) -> None:
        if self._stop_event is not None and self._stop_event.is_set():
            raise ProviderError("NH 자동 실행이 중지되어 주문을 전송하지 않습니다.")
        if not self._enabled or not self._session_verified or not self._cash_verified:
            raise ProviderError("NH 주문 전송이 비활성화되어 있거나 계좌·현금·세션 확인이 완료되지 않았습니다.")
        if not self._window()[0]:
            raise ProviderError("검증한 NH 주문 세션 밖이거나 미국 거래 달력을 확인할 수 없습니다.")

    def _request_json(self, path: str, fields: dict, token: str, *, cts: str = "", cts_flag: str = "") -> tuple[dict, dict]:
        if path not in _READ_PATHS | _WRITE_PATHS:
            raise ProviderError("허용되지 않은 NH 실행 API입니다.")
        url = _HOSTS[self.environment] + path
        parsed = urlsplit(url)
        expected = "api.nhplug.com" if self.environment == "live" else "moapi.nhplug.com"
        if parsed.scheme != "https" or parsed.hostname != expected or parsed.port != 8443 or parsed.query or parsed.fragment:
            raise ProviderError("허용되지 않은 NH 실행 주소입니다.")
        request = Request(url, data=_json_exact({"Input_0": fields}).encode("utf-8"), method="POST",
                          headers={"Authorization": "Bearer " + _header(token),
                                   "Content-Type": "application/json; charset=UTF-8", "Accept": "application/json"})
        if cts:
            request.add_header("cts", _header(cts))
        if cts_flag:
            request.add_header("cts_flag", _header(cts_flag))
        now = time.monotonic()
        if self._last_request_at is not None and now - self._last_request_at < 0.25:
            time.sleep(max(0, 0.25 - (now - self._last_request_at)))
        self._last_request_at = time.monotonic()
        if path in _WRITE_PATHS:
            self._write_guard()  # The session may have closed during the rate-limit wait.
        try:
            with self._provider._opener.open(request, timeout=15) as response:
                if response.geturl() != url:
                    raise ProviderError("NH 실행 응답의 주소 변경을 차단했습니다.")
                headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
                if response.status != 200:
                    raise _HTTPFailure(int(response.status), headers)
                raw = response.read(20_000_001)
            if not isinstance(raw, bytes) or len(raw) > 20_000_000:
                raise ProviderError("NH 실행 응답의 크기 또는 형식이 올바르지 않습니다.")
            payload = json.loads(raw.decode("utf-8"), parse_float=Decimal, parse_constant=_bad_json_constant)
            if not isinstance(payload, dict):
                raise ValueError
            return payload, headers
        except HTTPError as error:
            status, headers = int(error.code), {str(k).lower(): str(v) for k, v in (error.headers or {}).items()}
            error.close()
            raise _HTTPFailure(status, headers) from None
        except (URLError, OSError, ValueError, UnicodeError):
            raise ProviderError("NH 실행 응답을 확인할 수 없습니다.") from None

    def _read(self, path: str, fields: dict, *, cts: str = "", cts_flag: str = "") -> tuple[dict, dict]:
        if path not in _READ_PATHS:
            raise ProviderError("조회 경로에서 NH 주문을 전송할 수 없습니다.")
        token = self._provider._get_token()
        refreshed, retries = False, 0
        while True:
            try:
                payload, headers = self._request_json(path, fields, token, cts=cts, cts_flag=cts_flag)
                _business_check(payload)
                return payload, headers
            except _HTTPFailure as error:
                if error.status == 401 and not refreshed:
                    token = self._provider._get_token(force=True)
                    refreshed = True
                    continue
                if error.status == 429 and retries < 2:
                    time.sleep(_retry_delay(error.headers, retries))
                    retries += 1
                    continue
                raise ProviderError("NH 주문 전 조회에 실패했습니다. 연결과 조회 권한을 확인하세요.") from None

    def _pages(self, path: str, fields: dict):
        cursor = flag = ""
        seen = set()
        for _ in range(1000):
            payload, headers = self._read(path, fields, cts=cursor, cts_flag=flag)
            cursor, flag = _continuation(payload, headers)
            if cursor and cursor in seen:
                raise ProviderError("NH 연속조회가 반복되어 불완전한 결과를 사용하지 않습니다.")
            yield payload
            if not cursor:
                return
            seen.add(cursor)
        raise ProviderError("NH 연속조회가 완료되지 않았습니다.")

    def _quote_data(self, symbol: str) -> tuple[Quote, dict]:
        payload, _ = self._read(_QUOTE, {"iem_cd": symbol})
        row = payload.get("Output_0")
        if not isinstance(row, dict) or row.get("iem_cd") != symbol or row.get("currency_unit") != "USD":
            raise ProviderError("NH 시세의 종목 또는 통화가 일치하지 않습니다.")
        return Quote(symbol, _decimal(row.get("trdprc")), _utc(_now()), "USD"), row

    def _session_matches(self, row: dict) -> bool:
        return (row.get("marketperiod_cls") == ("1" if self.session == "regular" else "2")
                and _compact_date(row.get("trade_date")) == _utc(_now()).astimezone(NY).date())

    def quote(self, account_id: str, symbol: str) -> Quote:
        self._account_check(account_id)
        return self._quote_data(_us_code(symbol))[0]

    def _query_orders(self, day: str, *, symbol: str | None = None, unfilled: bool = False) -> list[dict]:
        fields = {"act_no": self._account, "orr_dt": day.replace("-", ""), "oss_sby_dit_cd": "0",
                  "sot_dit": "0", "ost_cns_dit": "2" if unfilled else "0"}
        if symbol is not None:
            fields["iem_cd"] = symbol
        result = []
        for payload in self._pages(_ORDERS, fields):
            for row in _output_rows(payload, "Output_0", "주문 상태"):
                if _compact_date(row.get("orr_dt")).isoformat() != day:
                    raise ProviderError("NH 주문 조회 날짜가 일치하지 않습니다.")
                result.append(row)
        return result

    def snapshot(self, account_id: str, symbols: tuple[str, ...]) -> BrokerSnapshot:
        self._account_check(account_id)
        symbols = tuple(dict.fromkeys(_us_code(symbol) for symbol in symbols))
        if not symbols:
            raise ProviderError("NH 검증 대상 종목이 없습니다.")
        holdings = {symbol: _ZERO for symbol in symbols}
        summary = None
        no_credit = True
        seen_positions = set()
        for payload in self._pages(_BALANCE, {"act_no": self._account, "qut_iqr_dit_cd": "9",
                "fc_sec_trd_nat_cd": "200", "cur_cd": "USD", "xns_dit_cd": "1"}):
            summary = payload.get("Output_0")
            if not isinstance(summary, dict):
                summary = None
            for row in _output_rows(payload, "Output_1", "보유 종목", allow_summary=bool(summary)):
                if row.get("fc_sec_trd_nat_cd") != "200" or row.get("cur_cd") != "USD":
                    raise ProviderError("NH 미국 현금 계좌의 국가·통화가 일치하지 않습니다.")
                symbol = _us_code(row.get("iem_cd"))
                quantity = _decimal(row.get("cns_bse_bnc_qty"), allow_zero=True)
                if quantity != quantity.to_integral_value():
                    no_credit = False  # This execution adapter supports whole-share holdings only.
                kind = str(row.get("cfd_lon_cd_nm", ""))
                if re.search(r"신용|대출|대주", kind) or row.get("lon_dt") not in (None, "", "00000000"):
                    no_credit = False
                identity = (symbol, kind, row.get("lon_dt"), row.get("xrn_dt"))
                if identity in seen_positions:
                    raise ProviderError("NH 보유 종목이 중복되어 잔고를 확정할 수 없습니다.")
                seen_positions.add(identity)
                if symbol in holdings:
                    holdings[symbol] += quantity
        if not summary or "fc_dca" not in summary:
            raise ProviderError("NH 외화 현금 잔고가 확인되지 않았습니다.")
        cash = _decimal(summary["fc_dca"], allow_zero=True)
        pending = set()
        for day in _candidate_dates(_utc(_now())):
            for row in self._query_orders(day, unfilled=True):
                remaining = _integer(row.get("ny_cns_orr_qty"), zero=True)
                if remaining:
                    pending.add(f"{day}:{_integer(row.get('orr_no'))}")
        market_open, close_at = self._window()
        for symbol in symbols:
            _, row = self._quote_data(symbol)
            market_open = self._session_matches(row) and market_open
        return BrokerSnapshot(self.account_id, _utc(_now()), holdings, cash, tuple(sorted(pending)),
                              market_open, close_at, bool(self._cash_verified and no_credit and market_open))

    def _validate_request(self, request: OrderRequest) -> tuple:
        self._account_check(request.account_id)
        if request.side not in {"BUY", "SELL"} or type(request.quantity) is not int:
            raise ProviderError("NH 주문은 매수·매도와 정수 수량이어야 합니다.")
        _integer(request.quantity)
        if _us_code(request.symbol) != request.symbol:
            raise ProviderError("NH 주문 종목은 대문자 티커여야 합니다.")
        price = _price(request.limit_price)
        if not isinstance(request.intent_id, str) or not re.fullmatch(r"[A-Za-z0-9_:.\-]{1,200}", request.intent_id):
            raise ProviderError("로컬 주문 의도의 식별자를 확인하세요.")
        return (request.account_id, request.symbol, request.side, request.quantity, price, self.environment, self.session)

    def _preflight(self, request: OrderRequest) -> None:
        _, quote = self._quote_data(request.symbol)
        if not self._session_matches(quote):
            raise ProviderError("NH 시세의 거래일·현재 시장 구분이 선택한 세션과 다릅니다.")
        payload, _ = self._read(_AVAILABLE, {"act_no": self._account, "pcs_dit": "2" if request.side == "BUY" else "3",
            "fc_sec_trd_nat_cd": "200", "iem_cd": request.symbol, "fc_orr_uit_pr": request.limit_price,
            "wtm_cur_knd_cd": "1", "oss_orr_knd_cd": "1", "ahi_nmn_pr_tp_cd": "00" if self.session == "regular" else "62",
            "cfd_lon_cd": "00"})
        row = payload.get("Output_0")
        if not isinstance(row, dict) or row.get("wtm_cur_cd") != "USD":
            raise ProviderError("NH 주문 가능 통화가 USD로 확인되지 않았습니다.")
        if request.side == "BUY":
            foreign_cash = _decimal(row.get("fc_dca"), allow_zero=True)
            reserved = _decimal(row.get("csh_wtm"), allow_zero=True)
            other_used = _decimal(row.get("ect_use_amt"), allow_zero=True)
            with localcontext() as context:
                context.prec = 50
                cash = min(max(_ZERO, foreign_cash - reserved - other_used), _decimal(row.get("orr_pbl_amt"), allow_zero=True))
                cost = request.quantity * request.limit_price
            possible = _integer(row.get("orr_pbl_qty"), zero=True)
            if cash < cost or possible < request.quantity:
                raise ProviderError("NH 현금 또는 일반 현금 매수 가능 수량이 부족합니다.")
        elif min(_integer(row.get("sll_pbl_qty"), zero=True), _integer(row.get("hld_qty"), zero=True)) < request.quantity:
            raise ProviderError("NH 일반 현금 매도 가능 수량이 부족합니다.")

    def _handle(self, request: OrderRequest, order_number: int, submitted: datetime) -> str:
        return _encode_handle({"account_id": self.account_id, "environment": self.environment, "order_number": str(order_number),
            "submitted_at": _utc(submitted).isoformat(), "symbol": request.symbol, "side": request.side,
            "quantity": request.quantity, "limit_price": _decimal_text(request.limit_price), "session": self.session})

    def submit_limit(self, request: OrderRequest) -> OrderUpdate:
        signature = self._validate_request(request)
        self._write_guard()
        with self._lock:
            previous = self._intents.get(request.intent_id)
            if previous:
                if previous[0] != signature:
                    raise ProviderError("같은 로컬 주문 의도에 다른 주문 내용이 지정되었습니다.")
                return previous[1]
        self._preflight(request)
        token = self._provider._get_token()  # Safe prefetch; never refresh after a write attempt.
        self._write_guard()
        fields = {"act_no": self._account, "fc_sec_trd_nat_cd": "200", "iem_cd": request.symbol,
                  "orr_qty": request.quantity, "fc_orr_uit_pr": request.limit_price,
                  "ahi_nmn_pr_tp_cd": "00" if self.session == "regular" else "62"}
        if request.side == "BUY":
            fields["wtm_cur_knd_cd"] = "1"
        unknown = OrderUpdate(None, "UNKNOWN", updated_at=_utc(_now()), message="주문 전송 결과가 불확실합니다. 재전송하지 말고 NH 내역을 대조하세요.")
        with self._lock:
            previous = self._intents.get(request.intent_id)
            if previous:
                if previous[0] != signature:
                    raise ProviderError("같은 로컬 주문 의도에 다른 주문 내용이 지정되었습니다.")
                return previous[1]
            self._intents[request.intent_id] = signature, unknown
        submitted = _utc(_now())
        try:
            payload, _ = self._request_json(_BUY if request.side == "BUY" else _SELL, fields, token)
            _business_check(payload)
            output = payload.get("Output_0")
            number = _integer(output.get("orr_no")) if isinstance(output, dict) else 0
            if not 0 < number <= 9999999999:
                raise ProviderError("NH 주문번호가 확인되지 않았습니다.")
            result = OrderUpdate(self._handle(request, number, submitted), "SUBMITTED", updated_at=_utc(_now()),
                                 message="NH 주문번호를 받았습니다. 실제 체결은 별도 조회로 확인합니다.")
        except (_HTTPFailure, ProviderError):
            result = unknown
        with self._lock:
            self._intents[request.intent_id] = signature, result
        return result

    def _context(self, account_id: str, handle: str) -> dict:
        self._account_check(account_id)
        context = decode_order_handle(handle)
        if context["account_id"] != self.account_id or context["environment"] != self.environment:
            raise ProviderError("NH 주문 식별자의 계좌 또는 환경이 현재 연결과 다릅니다.")
        return context

    def order_status(self, account_id: str, broker_order_id: str) -> OrderUpdate:
        context = self._context(account_id, broker_order_id)
        unknown = OrderUpdate(broker_order_id, "UNKNOWN", updated_at=_utc(_now()), message="NH 주문 상태를 유일하게 확인하지 못했습니다.")
        try:
            rows = []
            for day in context["candidate_dates"]:
                rows.extend(self._query_orders(day, symbol=context["symbol"]))
            all_unique = {}
            for row in rows:
                key = (row["orr_dt"], _integer(row.get("orr_no")))
                if key in all_unique and all_unique[key] != row:
                    return unknown
                all_unique[key] = row
            rows = list(all_unique.values())
            roots = [row for row in rows if _integer(row.get("orr_no")) == int(context["order_number"])]
            unique = {}
            for row in roots:
                unique[(row["orr_dt"], str(row["orr_no"]))] = row
            if len(unique) != 1:
                return unknown
            row = next(iter(unique.values()))
            if (row.get("iem_cd") != context["symbol"] or row.get("fc_sec_trd_nat_cd") != "200"
                    or row.get("oss_sby_dit_cd") != ("2" if context["side"] == "BUY" else "1")
                    or _integer(row.get("orr_qty")) != context["quantity"]
                    or _decimal(row.get("fc_orr_uit_pr")) != Decimal(context["limit_price"])
                    or row.get("cfd_lon_cd") != "00" or _integer(row.get("org_orr_no"), zero=True) != 0):
                return unknown
            lineage = {int(context["order_number"])}
            related = []
            while True:
                found = [item for item in rows if item["orr_dt"] == row["orr_dt"]
                         and _integer(item.get("orr_no")) not in lineage
                         and _integer(item.get("org_orr_no"), zero=True) in lineage]
                if not found:
                    break
                related.extend(found)
                lineage.update(_integer(item["orr_no"]) for item in found)
            related_ids = tuple(sorted({str(_integer(item.get("orr_no"))) for item in related}))
            if _integer(row.get("cor_qty"), zero=True) or any(
                    _integer(item.get("cns_qty"), zero=True) or _integer(item.get("ny_cns_orr_qty"), zero=True)
                    for item in related):
                return OrderUpdate(broker_order_id, "UNKNOWN", updated_at=_utc(_now()), related_order_ids=related_ids,
                                   message="정정·연결 주문의 누적 체결 관계를 확인해야 합니다.")
            filled = _integer(row.get("cns_qty"), zero=True)
            remaining = _integer(row.get("ny_cns_orr_qty"), zero=True)
            cancelled = _integer(row.get("can_qty"), zero=True)
            state = row.get("oss_ato_orr_sts_cd")
            average = _decimal(row.get("cns_pr")) if filled else None
            if state == "2" and not filled and not remaining:
                status = "REJECTED"
            elif state not in {"0", "1", "3"} or filled + remaining + cancelled != context["quantity"]:
                return unknown
            elif filled == context["quantity"] and not remaining and not cancelled:
                status = "FILLED"
            elif cancelled and not remaining:
                status = "CANCELLED"
            elif filled and remaining:
                status = "PARTIAL"
            elif not filled and remaining:
                status = "SUBMITTED"
            else:
                return unknown
            return OrderUpdate(broker_order_id, status, Decimal(filled), average, _utc(_now()),
                               cancelled > 0, related_ids, "NH 주문 수량·체결·잔량을 대조한 상태입니다.")
        except (ProviderError, TypeError, KeyError):
            return unknown

    def cancel(self, account_id: str, broker_order_id: str) -> OrderUpdate:
        context = self._context(account_id, broker_order_id)
        self._write_guard()
        with self._lock:
            if broker_order_id in self._cancels:
                return self._cancels[broker_order_id]
        previous = self.order_status(account_id, broker_order_id)
        if previous.status not in {"SUBMITTED", "PARTIAL"}:
            return previous
        token = self._provider._get_token()
        self._write_guard()
        unknown = OrderUpdate(broker_order_id, "UNKNOWN", previous.filled_quantity, previous.average_price,
                              _utc(_now()), True, message="취소 전송 결과가 불확실합니다. 취소를 반복하지 말고 주문 상태를 확인하세요.")
        with self._lock:
            if broker_order_id in self._cancels:
                return self._cancels[broker_order_id]
            self._cancels[broker_order_id] = unknown
        try:
            payload, _ = self._request_json(_CANCEL, {"act_no": self._account, "org_orr_no": int(context["order_number"]),
                "fc_sec_trd_nat_cd": "200", "iem_cd": context["symbol"], "all_pat_dit_cd": "1"}, token)
            _business_check(payload)
            output = payload.get("Output_0")
            number = _integer(output.get("orr_no")) if isinstance(output, dict) else 0
            if not 0 < number <= 9999999999:
                raise ProviderError("NH 취소 접수 번호가 확인되지 않았습니다.")
            result = OrderUpdate(broker_order_id, previous.status, previous.filled_quantity, previous.average_price,
                                 _utc(_now()), True, (str(number),), "취소 접수 응답입니다. 잔량 소멸은 별도 조회로 확인합니다.")
        except (_HTTPFailure, ProviderError):
            result = unknown
        with self._lock:
            self._cancels[broker_order_id] = result
        return result
