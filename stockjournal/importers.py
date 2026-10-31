"""Preview-only importers for the journal's legacy workbook and plain files.

Importing never writes the journal.  Every questionable value is retained for
the review dialog, and a ``_blocked`` row must be repaired before committing.
Fields whose names start with ``_`` belong only to the preview UI.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


@dataclass
class ImportPreview:
    accounts: list[dict[str, Any]] = field(default_factory=list)
    instruments: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    dividends: list[dict[str, Any]] = field(default_factory=list)
    dividend_events: list[dict[str, Any]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


_MARKETS = {
    "KR": "KR", "KRX": "KR", "KOSPI": "KR", "KOSDAQ": "KR",
    "KONEX": "KR", "한국": "KR", "국내": "KR", "한국주식": "KR",
    "US": "US", "USA": "US", "NASDAQ": "US", "NYSE": "US",
    "AMEX": "US", "미국": "US", "미국주식": "US",
    "JP": "JP", "일본": "JP", "HK": "HK", "홍콩": "HK",
    "CN": "CN", "중국": "CN", "GB": "GB", "UK": "GB",
    "EU": "EU", "CA": "CA", "AU": "AU", "SG": "SG",
}
_CURRENCIES = {"KR": "KRW", "US": "USD", "JP": "JPY", "HK": "HKD", "CN": "CNY", "GB": "GBP", "EU": "EUR", "CA": "CAD", "AU": "AUD", "SG": "SGD"}
_VALID_CURRENCIES = set(_CURRENCIES.values()) | {"CHF", "TWD", "NZD"}
_ALIASES = {
    "account": ("account", "account_name", "계좌", "계좌명", "증권계좌"),
    "account_currency": ("account_currency", "계좌통화"),
    "broker": ("broker", "증권사"),
    "kind": ("kind", "account_type", "계좌유형"),
    "code": ("code", "ticker", "ticket", "symbol", "종목코드", "종목번호", "코드"),
    "name": ("name", "종목명", "주식명"),
    "market": ("market", "exchange", "시장", "거래소"),
    "currency": ("currency", "통화"),
    "date": ("date", "executed_at", "trade_date", "거래일", "거래일자", "체결일", "체결일자", "체결일시", "매매일", "날짜", "일자"),
    "side": ("side", "거래유형", "구분", "매매구분", "거래구분", "매수매도"),
    "quantity": ("quantity", "qty", "shares", "수량", "거래수량", "체결수량", "주수"),
    "price": ("price", "unit_price", "단가", "가격", "거래단가", "체결가", "매매단가"),
    "fee": ("fee", "commission", "수수료"),
    "tax": ("tax", "withholding_tax", "세금", "제세금", "거래세", "원천징수", "원천징수세액"),
    "external_id": ("external_id", "trade_id", "fill_id", "id", "거래번호", "체결번호", "주문번호"),
    "source": ("source", "출처"),
    "memo": ("memo", "note", "notes", "비고", "메모"),
    "asset_type": ("asset_type", "자산유형", "종류", "유형"),
    "sector": ("sector", "업종", "섹터"),
    "gross": ("gross", "gross_amount", "gross_dividend", "배당금", "총배당금", "세전배당금"),
    "net": ("net", "net_amount", "net_dividend", "실수령액", "세후배당금"),
    "paid_date": ("paid_date", "payment_date", "지급일", "입금일", "배당지급일"),
    "amount_per_share": ("amount_per_share", "dividend_per_share", "주당배당금", "주당배당액", "배당금(주당)"),
    "record_date": ("record_date", "기준일", "배당기준일"),
    "pay_date": ("pay_date", "예정지급일", "배당예정일", "지급예정일"),
    "ex_date": ("ex_date", "ex_dividend_date", "배당락일"),
    "sequence": ("sequence", "순서", "체결순서"),
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _header(value: Any) -> str:
    return re.sub(r"[\s_\-()[\]{}]", "", unicodedata.normalize("NFKC", _text(value))).casefold()


_HEADER_LOOKUP = {_header(alias): name for name, aliases in _ALIASES.items() for alias in aliases}


def _digest(prefix: str, value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_text).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()[:32]}"


def _decimal(value: Any, label: str, *, positive: bool = False) -> tuple[str, list[str]]:
    raw = _text(value)
    if not raw:
        return "", [f"{label}이(가) 없습니다."]
    normalized = unicodedata.normalize("NFKC", raw).strip()
    try:
        if len(normalized) > 128:
            raise InvalidOperation
        if "," in normalized and not re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", normalized):
            raise InvalidOperation
        normalized = normalized.replace(",", "")
        if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", normalized):
            raise InvalidOperation
        number = Decimal(normalized)
        if not number.is_finite() or number < 0 or (positive and number == 0):
            raise InvalidOperation
        if number != 0 and (number.adjusted() > 40 or number.adjusted() < -40):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        condition = "0보다 큰 유한한 숫자" if positive else "0 이상의 유한한 숫자"
        return raw, [f"{label}은(는) {condition}여야 합니다: {raw}"]
    result = format(number, "f") if number else "0"
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result or "0", []


def _date(value: Any, label: str, *, only_date: bool = False, epoch: Any = None) -> tuple[str, list[str]]:
    raw = _text(value)
    if not raw:
        return "", [f"{label}이(가) 없습니다."]
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime.combine(value, datetime.min.time())
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and 1 <= value <= 2958465:
            from openpyxl.utils.datetime import WINDOWS_EPOCH, from_excel

            parsed = from_excel(value, epoch=epoch or WINDOWS_EPOCH)
            if not isinstance(parsed, datetime):
                raise ValueError
        else:
            normalized = raw.replace("년", "-").replace("월", "-").replace("일", "")
            normalized = re.sub(r"^(\d{4})[./](\d{1,2})[./](\d{1,2})", r"\1-\2-\3", normalized)
            match = re.fullmatch(r"(\d{4})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})", normalized.strip())
            if match:
                parsed = datetime(*(int(part) for part in match.groups()))
            else:
                parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        return parsed.date().isoformat() if only_date else parsed.isoformat(), []
    except (ValueError, OverflowError, TypeError):
        return raw, [f"{label} 형식을 확인하세요 (YYYY-MM-DD): {raw}"]


def _code_and_market(code: Any, market: Any = "") -> tuple[str, str, list[str]]:
    raw = unicodedata.normalize("NFKC", _text(code)).upper().lstrip("'").strip()
    market_raw = _text(market).upper()
    if ":" in raw:
        supplied_market, raw = raw.split(":", 1)
        market_raw = market_raw or supplied_market
    if raw.endswith((".KS", ".KQ")):
        raw = raw[:-3]
        market_raw = market_raw or "KR"
    if re.fullmatch(r"\d+\.0", raw):
        raw = raw[:-2]
    if raw.isdigit() and len(raw) <= 6 and (not market_raw or _MARKETS.get(market_raw) == "KR"):
        raw = raw.zfill(6)
    if not market_raw:
        if re.fullmatch(r"[0-9A-Z]{6}", raw) and any(character.isdigit() for character in raw):
            market_raw = "KR"
        elif re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", raw):
            market_raw = "US"
    canonical_market = _MARKETS.get(market_raw, "")
    errors = []
    if not raw:
        errors.append("종목코드가 없습니다.")
    elif not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,24}", raw):
        errors.append(f"종목코드를 확인하세요: {raw}")
    if not canonical_market:
        errors.append(f"시장을 확인하세요: {market_raw or '(미입력)'}")
    elif canonical_market == "KR" and not re.fullmatch(r"[0-9A-Z]{6}", raw):
        errors.append(f"국내 종목코드는 영문/숫자 6자리여야 합니다: {raw}")
    return raw, canonical_market or market_raw, errors


class _Builder:
    def __init__(self) -> None:
        self.preview = ImportPreview()
        self.now = datetime.now(timezone.utc).isoformat()
        self.accounts: dict[str, dict[str, Any]] = {}
        self.instruments: dict[str, dict[str, Any]] = {}
        self.occurrences: Counter[str] = Counter()
        self.explicit_ids: dict[str, list[dict[str, Any]]] = {}

    def metadata(self, identity: str) -> dict[str, Any]:
        return {"id": identity, "updated_at": self.now, "deleted": False}

    def block(self, row: dict[str, Any], message: str) -> None:
        row["_blocked"] = True
        if message not in row["_warnings"]:
            row["_warnings"].append(message)
        issue = f"{row.get('_source_row', '가져오기')}: {message}"
        if issue not in self.preview.issues:
            self.preview.issues.append(issue)

    def review(self, row: dict[str, Any], source_row: str, raw: Any, errors: list[str], warnings: list[str] | None = None) -> None:
        row.update(_source_row=source_row, _raw=raw, _blocked=False, _warnings=list(warnings or []))
        for error in errors:
            self.block(row, error)

    def instrument(self, raw: dict[str, Any]) -> tuple[str, str, list[str]]:
        code, market, errors = _code_and_market(raw.get("code"), raw.get("market"))
        currency = _text(raw.get("currency")).upper()
        currency = {"원": "KRW", "달러": "USD", "$": "USD", "₩": "KRW"}.get(currency, currency)
        currency = currency or _CURRENCIES.get(market, "")
        if currency not in _VALID_CURRENCIES:
            errors.append(f"통화를 확인하세요: {currency or '(미입력)'}")
        if errors:
            return "", currency, errors
        identity = f"{market}:{code}"
        instrument = self.instruments.get(identity)
        if instrument is None:
            instrument = {
                **self.metadata(identity), "market": market, "code": code,
                "name": _text(raw.get("name")) or code, "currency": currency,
                "asset_type": _text(raw.get("asset_type")) or "stock", "sector": _text(raw.get("sector")),
            }
            self.instruments[identity] = instrument
            self.preview.instruments.append(instrument)
        else:
            for key in ("name", "asset_type", "sector"):
                supplied = _text(raw.get(key))
                if supplied and (not instrument[key] or (key == "name" and instrument[key] == code) or (key == "asset_type" and instrument[key] == "stock")):
                    instrument[key] = supplied
            if instrument["currency"] != currency:
                errors.append(f"같은 종목의 통화가 다릅니다: {identity} ({instrument['currency']} / {currency})")
        return identity, currency, errors

    def account(self, raw: dict[str, Any], currency: str) -> tuple[str, list[str]]:
        name = " ".join(unicodedata.normalize("NFKC", _text(raw.get("account"))).split())
        if not name:
            return "", ["계좌가 없습니다."]
        identity = _digest("account", name.casefold())
        if identity not in self.accounts:
            account = {
                **self.metadata(identity), "name": name, "broker": _text(raw.get("broker")),
                "kind": _text(raw.get("kind")) or "brokerage",
                "currency": _text(raw.get("account_currency")).upper() or currency or "KRW",
            }
            self.accounts[identity] = account
            self.preview.accounts.append(account)
        return identity, []

    def identity(self, kind: str, row: dict[str, Any], date_field: str, *, forced: str = "") -> None:
        external_id = row.get("external_id", "")
        scope = [kind, row.get("account_id", ""), row.get(date_field, "")[:10], row.get("source", ""), external_id]
        if external_id:
            identity = forced or _digest(kind, scope)
            key = _digest("explicit", scope)
            earlier = self.explicit_ids.setdefault(key, [])
            if earlier:
                message = f"같은 계좌/날짜/출처에 중복된 명시적 ID가 있습니다: {external_id}"
                for previous in earlier:
                    self.block(previous, message)
                self.block(row, message)
            earlier.append(row)
        else:
            content = {key: value for key, value in row.items() if not key.startswith("_") and key not in {"id", "updated_at", "deleted", "sequence"}}
            fingerprint = _digest(kind, content)
            self.occurrences[fingerprint] += 1
            identity = forced or _digest(kind, [content, self.occurrences[fingerprint]])
        row.update(self.metadata(identity))

    def costs(self, raw: dict[str, Any], keys: tuple[str, ...] = ("fee", "tax")) -> tuple[dict[str, str], list[str], list[str]]:
        values, errors, warnings = {}, [], []
        for key in keys:
            label = {"fee": "수수료", "tax": "세금"}[key]
            if key not in raw:
                values[key] = "0"
                warnings.append(f"원본에 {label} 항목이 없어 0으로 표시했습니다. 실제 비용을 확인하세요.")
            else:
                values[key], found = _decimal(raw[key], label)
                errors.extend(found)
        return values, errors, warnings

    def trade(self, raw: dict[str, Any], source: str, row_number: int, *, extra_errors: list[str] | None = None, extra_warnings: list[str] | None = None, forced_id: str = "", epoch: Any = None) -> dict[str, Any]:
        instrument_id, currency, errors = self.instrument(raw)
        account_id, found = self.account(raw, currency)
        errors.extend(found)
        executed_at, found = _date(raw.get("date"), "거래일", epoch=epoch)
        errors.extend(found)
        side_raw = _text(raw.get("side")).upper()
        side = {"BUY": "BUY", "매수": "BUY", "매입": "BUY", "SELL": "SELL", "매도": "SELL", "매각": "SELL"}.get(side_raw, side_raw)
        if side not in {"BUY", "SELL"}:
            errors.append(f"매매구분을 확인하세요 (BUY/SELL 또는 매수/매도): {side_raw or '(미입력)'}")
        quantity, found = _decimal(raw.get("quantity"), "수량", positive=True)
        errors.extend(found)
        price, found = _decimal(raw.get("price"), "단가", positive=True)
        errors.extend(found)
        costs, found, warnings = self.costs(raw)
        errors.extend(found)
        sequence = row_number
        if "sequence" in raw:
            try:
                sequence_number = Decimal(_text(raw["sequence"]))
                if not sequence_number.is_finite() or sequence_number < 0 or sequence_number != sequence_number.to_integral_value():
                    raise ValueError
                sequence = int(sequence_number)
            except (ValueError, InvalidOperation):
                errors.append(f"체결순서는 0 이상의 정수여야 합니다: {_text(raw['sequence'])}")
        row = {
            "account_id": account_id, "instrument_id": instrument_id, "executed_at": executed_at,
            "side": side, "quantity": quantity, "price": price, **costs,
            "currency": currency, "sequence": sequence, "source": _text(raw.get("source")) or source,
            "external_id": _text(raw.get("external_id")), "memo": _text(raw.get("memo")),
        }
        self.review(row, f"{source} {row_number}행", dict(raw), errors + list(extra_errors or []), warnings + list(extra_warnings or []))
        self.identity("trade", row, "executed_at", forced=forced_id)
        self.preview.trades.append(row)
        return row

    def dividend(self, raw: dict[str, Any], source: str, row_number: int, *, extra_errors: list[str] | None = None, epoch: Any = None) -> dict[str, Any]:
        instrument_id, currency, errors = self.instrument(raw)
        account_id, found = self.account(raw, currency)
        errors.extend(found)
        paid_date, found = _date(raw.get("paid_date", raw.get("date")), "배당 지급일", only_date=True, epoch=epoch)
        errors.extend(found)
        gross, found = _decimal(raw.get("gross"), "세전 배당금")
        errors.extend(found)
        costs, found, warnings = self.costs(raw, ("tax",))
        errors.extend(found)
        row = {"account_id": account_id, "instrument_id": instrument_id, "paid_date": paid_date,
               "gross": gross, **costs, "currency": currency,
               "source": _text(raw.get("source")) or source, "external_id": _text(raw.get("external_id")), "memo": _text(raw.get("memo"))}
        if "net" in raw:
            row["net"], found = _decimal(raw["net"], "세후 배당금")
            errors.extend(found)
            if not errors and Decimal(row["net"]) != Decimal(gross) - Decimal(costs["tax"]):
                errors.append("세후 배당금이 세전 배당금 - 세금과 일치하지 않습니다.")
        if not errors and Decimal(costs["tax"]) > Decimal(gross):
            errors.append("배당 세금이 세전 배당금보다 큽니다.")
        self.review(row, f"{source} {row_number}행", dict(raw), errors + list(extra_errors or []), warnings)
        self.identity("dividend", row, "paid_date")
        self.preview.dividends.append(row)
        return row

    def event(self, raw: dict[str, Any], source: str, row_number: int, *, extra_errors: list[str] | None = None, epoch: Any = None) -> dict[str, Any]:
        instrument_id, currency, errors = self.instrument(raw)
        amount, found = _decimal(raw.get("amount_per_share"), "주당 배당금")
        errors.extend(found)
        dates = {"record_date": "", "pay_date": "", "ex_date": ""}
        for key, label in (("record_date", "배당 기준일"), ("pay_date", "배당 예정 지급일"), ("ex_date", "배당락일")):
            value = raw.get(key)
            if key == "pay_date" and not value:
                value = raw.get("paid_date")
            if _text(value):
                dates[key], found = _date(value, label, only_date=True, epoch=epoch)
                errors.extend(found)
        if not dates["record_date"] and not dates["pay_date"]:
            errors.append("배당 기준일 또는 예정 지급일이 필요합니다.")
        row = {"instrument_id": instrument_id, "amount_per_share": amount, "currency": currency, "status": "reference", **dates,
               "source": _text(raw.get("source")) or source, "external_id": _text(raw.get("external_id")), "memo": _text(raw.get("memo"))}
        self.review(row, f"{source} {row_number}행", dict(raw), errors + list(extra_errors or []))
        self.identity("dividend_event", row, "record_date" if dates["record_date"] else "pay_date")
        self.preview.dividend_events.append(row)
        return row


def _mapped_headers(values: list[Any]) -> tuple[dict[int, str], list[str]]:
    mapping = {}
    duplicates = []
    seen = set()
    for index, value in enumerate(values):
        canonical = _HEADER_LOOKUP.get(_header(value))
        if canonical:
            if canonical in seen:
                duplicates.append(f"같은 의미의 열이 중복되었습니다: {_text(value)} ({canonical})")
            else:
                mapping[index] = canonical
                seen.add(canonical)
    return mapping, duplicates


def _table_kind(mapping: dict[int, str]) -> str:
    fields = set(mapping.values())
    if "amount_per_share" in fields:
        return "event"
    if "gross" in fields or "paid_date" in fields:
        return "dividend"
    return "trade"


def preview_csv(path: str | Path) -> ImportPreview:
    """Read UTF-8/BOM or CP949 CSV without changing any persisted data."""
    builder = _Builder()
    payload = Path(path).read_bytes()
    try:
        contents = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            contents = payload.decode("cp949")
        except UnicodeDecodeError as exc:
            raise ValueError("CSV 파일은 UTF-8 또는 CP949 인코딩이어야 합니다.") from exc
    if not contents.strip():
        builder.preview.issues.append("CSV 파일이 비어 있습니다.")
        return builder.preview
    try:
        dialect = csv.Sniffer().sniff(contents[:8192], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = csv.reader(io.StringIO(contents, newline=""), dialect)
    headers = next(rows, [])
    mapping, errors = _mapped_headers(headers)
    if not mapping:
        errors.append("인식할 수 있는 CSV 열 제목이 없습니다.")
    kind = _table_kind(mapping)
    for row_number, values in enumerate(rows, 2):
        if not any(_text(value) for value in values):
            continue
        raw = {name: values[index] if index < len(values) else "" for index, name in mapping.items()}
        raw["_original_columns"] = dict(zip((str(header) for header in headers), values))
        row_errors = list(errors)
        if len(values) > len(headers):
            row_errors.append("열 제목보다 데이터 열이 많습니다. 구분자와 따옴표를 확인하세요.")
        getattr(builder, kind)(raw, "csv", row_number, extra_errors=row_errors)
    if not (builder.preview.trades or builder.preview.dividends or builder.preview.dividend_events):
        builder.preview.issues.extend(errors or ["CSV에 가져올 데이터 행이 없습니다."])
    return builder.preview


def _cell_value(sheet: Any, formula_sheet: Any, row_number: int, column: int, *, required: bool = False) -> tuple[Any, list[str]]:
    cached = sheet.cell(row_number, column).value
    formula_cell = formula_sheet.cell(row_number, column)
    errors = []
    if required and formula_cell.data_type == "f" and cached is None:
        errors.append(f"{formula_sheet.title}!{formula_cell.coordinate} 수식의 계산 결과가 없습니다. Excel에서 다시 계산하거나 직접 값을 입력하세요.")
    return cached, errors


def _legacy_instruments(builder: _Builder, sheet: Any, formulas: Any) -> None:
    for row_number in range(1, sheet.max_row + 1):
        code, errors = _cell_value(sheet, formulas, row_number, 4, required=True)
        name = sheet.cell(row_number, 5).value
        if not _text(code) and not _text(name):
            continue
        if _header(code) in {_header(value) for value in _ALIASES["code"]}:
            continue
        raw = {"code": code, "name": name, "asset_type": sheet.cell(row_number, 2).value, "sector": sheet.cell(row_number, 3).value}
        _, _, found = builder.instrument(raw)
        for issue in errors + found:
            builder.preview.issues.append(f"종목 {row_number}행: {issue}")


def _legacy_trades(builder: _Builder, sheet: Any, formulas: Any, epoch: Any) -> None:
    source = "xlsx:매매일지"
    for row_number in range(1, sheet.max_row + 1):
        values = {column: _cell_value(sheet, formulas, row_number, column, required=column in {1, 2, 6, 8, 9}) for column in (1, 2, 6, 7, 8, 9, 11, 19, 20, 21)}
        if not any(_text(values[column][0]) or formulas.cell(row_number, column).data_type == "f" for column in (1, 2, 6, 7, 8, 9)):
            continue
        if _header(values[2][0]) in {_header(value) for value in _ALIASES["code"]} or _header(values[1][0]) in {_header(value) for value in _ALIASES["date"]}:
            continue
        raw = {"date": values[1][0], "code": values[2][0], "account": values[6][0], "name": values[7][0],
               "quantity": values[8][0], "price": values[9][0], "side": "BUY", "memo": values[21][0],
               "external_id": f"매매일지:{row_number}:BUY", "sequence": row_number * 2}
        errors = [error for _, found in values.values() for error in found]
        buy_date, _ = _date(raw["date"], "매수일", epoch=epoch)
        code, market, _ = _code_and_market(raw["code"])
        quantity, _ = _decimal(raw["quantity"], "수량", positive=True)
        price, _ = _decimal(raw["price"], "단가", positive=True)
        account = " ".join(_text(raw["account"]).split()).casefold()
        lot_id = _digest("lot", [sheet.title, row_number, buy_date, market, code, account, quantity, price])
        buy_id = _digest("trade", [lot_id, "BUY"])
        buy = builder.trade(raw, source, row_number, extra_errors=errors, forced_id=buy_id, epoch=epoch)
        buy["lot_id"] = lot_id
        closed = re.sub(r"\s+", "", _text(values[19][0])) == "처리완료"
        if not closed:
            continue
        sell_errors = []
        sell_date, found = _cell_value(sheet, formulas, row_number, 20, required=True)
        sell_errors.extend(found)
        exit_price, found = _cell_value(sheet, formulas, row_number, 11, required=True)
        sell_errors.extend(found)
        if formulas.cell(row_number, 11).data_type == "f":
            sell_errors.append("K열 매도 후보 가격이 수식입니다. 실제 매도 체결가를 직접 확인해야 합니다.")
        canonical_sell_date, found = _date(sell_date, "매도일", epoch=epoch)
        sell_errors.extend(found)
        if not found and not _date(raw["date"], "매수일", epoch=epoch)[1] and canonical_sell_date[:10] < buy_date[:10]:
            sell_errors.append("매도일이 매수일보다 빠릅니다.")
        sell_raw = {**raw, "date": sell_date, "price": exit_price, "side": "SELL", "external_id": f"매매일지:{row_number}:SELL", "sequence": row_number * 2 + 1}
        sell = builder.trade(sell_raw, source, row_number, extra_errors=errors + sell_errors,
                             extra_warnings=["처리 완료 행의 K열을 매도 단가 후보로 사용했습니다. 실제 체결가와 매도일을 확인하세요."],
                             forced_id=_digest("trade", [lot_id, "SELL"]), epoch=epoch)
        sell.update(lot_id=lot_id, linked_buy_id=buy["id"])
        # A legacy row is one purchase lot. Never import only half of an invalid
        # completed lot: that would manufacture either an open position or P/L.
        if buy["_blocked"] or sell["_blocked"]:
            for message in list(buy["_warnings"] if buy["_blocked"] else []) + list(sell_errors):
                if not message.startswith("원본에"):
                    builder.block(buy, message)
                    builder.block(sell, message)
            builder.block(buy, "연결된 매수/매도 행을 함께 확인해야 합니다.")
            builder.block(sell, "연결된 매수/매도 행을 함께 확인해야 합니다.")


def _legacy_events(builder: _Builder, sheet: Any, formulas: Any, epoch: Any) -> None:
    for row_number in range(1, sheet.max_row + 1):
        values = [_cell_value(sheet, formulas, row_number, column, required=column in {1, 2, 4}) for column in range(1, 5)]
        if not any(_text(value) for value, _ in values):
            continue
        if _header(values[1][0]) in {_header(value) for value in _ALIASES["code"]}:
            continue
        raw = {"record_date": values[0][0], "code": values[1][0], "name": values[2][0], "amount_per_share": values[3][0]}
        errors = [error for _, found in values for error in found]
        builder.event(raw, "xlsx:배당정보", row_number, extra_errors=errors, epoch=epoch)


def _generic_sheet(builder: _Builder, sheet: Any, formulas: Any, epoch: Any) -> bool:
    selected = None
    for row_number in range(1, min(sheet.max_row, 25) + 1):
        mapping, errors = _mapped_headers([cell.value for cell in sheet[row_number]])
        fields = set(mapping.values())
        if "code" in fields and (fields & {"side", "quantity", "gross", "amount_per_share"}):
            selected = row_number, mapping, errors
            break
    if selected is None:
        return False
    header_row, mapping, header_errors = selected
    kind = _table_kind(mapping)
    for row_number in range(header_row + 1, sheet.max_row + 1):
        raw, errors = {}, list(header_errors)
        any_content = False
        for index, field_name in mapping.items():
            value, found = _cell_value(sheet, formulas, row_number, index + 1, required=True)
            raw[field_name] = value
            errors.extend(found)
            any_content = any_content or bool(_text(value)) or formulas.cell(row_number, index + 1).data_type == "f"
        if any_content:
            getattr(builder, kind)(raw, f"xlsx:{sheet.title}", row_number, extra_errors=errors, epoch=epoch)
    return True


def preview_workbook(path: str | Path) -> ImportPreview:
    """Read the known Korean workbook layout or an explicitly headed table.

    Both formula and cached-value views are loaded.  openpyxl does not calculate
    formulas; missing caches are reported and never replaced with zero.
    """
    from openpyxl import load_workbook

    builder = _Builder()
    values = load_workbook(Path(path), data_only=True, read_only=False, keep_links=False)
    try:
        formulas = load_workbook(Path(path), data_only=False, read_only=False, keep_links=False)
        try:
            legacy_layout = '종목' in values.sheetnames and '매매일지' in values.sheetnames
            if "종목" in values.sheetnames:
                _legacy_instruments(builder, values["종목"], formulas["종목"])
            for sheet in values:
                if sheet.title == "종목":
                    continue
                if legacy_layout and sheet.title not in ('매매일지', '배당정보'):
                    builder.preview.issues.append(f"'{sheet.title}' 보조 시트는 가져오지 않았습니다. 기존 형식은 종목·매매일지·배당정보만 읽어 집계값 중복을 방지합니다.")
                    continue
                if sheet.title == "매매일지":
                    _legacy_trades(builder, sheet, formulas[sheet.title], values.epoch)
                elif sheet.title == "배당정보":
                    _legacy_events(builder, sheet, formulas[sheet.title], values.epoch)
                elif not _generic_sheet(builder, sheet, formulas[sheet.title], values.epoch):
                    if any(any(cell.value is not None for cell in row) for row in sheet):
                        builder.preview.issues.append(f"'{sheet.title}' 시트의 열 구조를 인식하지 못했습니다. 원본 파일은 변경되지 않았습니다.")
        finally:
            formulas.close()
    finally:
        values.close()
    return builder.preview


_SIDE_PATTERN = re.compile(r"매수|매도|\bBUY\b|\bSELL\b", re.IGNORECASE)
_NUMBER = r"[+-]?(?:\d[\d,]*)(?:\.\d+)?"


def preview_text(text: str, default_account: str = "", default_date: str | date | None = None) -> ImportPreview:
    """Parse one Korean/English fill per line, retaining unrecognized lines.

    An omitted date is resolved before identity generation. Repeated identical
    lines represent separate fills; explicit IDs are checked for conflicts.
    """
    builder = _Builder()
    effective_date = _text(default_date) or date.today().isoformat()
    for row_number, original in enumerate(text.splitlines(), 1):
        line = original.strip()
        if not line:
            continue
        side_match = _SIDE_PATTERN.search(line)
        if not side_match:
            builder.preview.issues.append(f"텍스트 {row_number}행: 거래로 해석할 수 없습니다. 원문: {original}")
            continue
        parse_errors = []
        if len(_SIDE_PATTERN.findall(line)) > 1:
            parse_errors.append("한 줄에 여러 매매구분이 있습니다. 거래를 한 줄에 하나씩 나누세요.")
        if len(re.findall(rf"({_NUMBER})\s*(?:주|shares?\b)", line, re.IGNORECASE)) > 1:
            parse_errors.append("한 줄에 여러 수량이 있습니다. 실제 체결 수량을 확인하세요.")
        if len(re.findall(rf"(?:[@$₩]\s*({_NUMBER})|({_NUMBER})\s*(원|달러|USD\b|KRW\b))", line, re.IGNORECASE)) > 1:
            parse_errors.append("한 줄에 여러 금액이 있습니다. 실제 체결 단가를 확인하세요.")
        spans = [side_match.span()]
        date_match = re.search(r"\b\d{4}[-./]\d{1,2}[-./]\d{1,2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?", line)
        quantity_match = re.search(rf"({_NUMBER})\s*(?:주|shares?\b)", line, re.IGNORECASE)
        price_match = re.search(rf"(?:[@$₩]\s*({_NUMBER})|({_NUMBER})\s*(원|달러|USD\b|KRW\b))", line, re.IGNORECASE)
        account_match = re.search(r"(?:계좌|account)\s*[:=]\s*([^\s,;]+)", line, re.IGNORECASE)
        bracket_account = re.match(r"\[([^\]]+)\]", line) if not account_match else None
        id_match = re.search(r"(?:external_id|체결번호|거래번호)\s*[:=]\s*([^\s,;]+)", line, re.IGNORECASE)
        for match in (date_match, quantity_match, price_match, account_match, bracket_account, id_match):
            if match:
                spans.append(match.span())
        masked = list(line)
        for start, end in spans:
            masked[start:end] = " " * (end - start)
        residual = "".join(masked)
        code_match = re.search(r"(?<![A-Za-z0-9])(?:KR:|KRX:|US:)?(?:[0-9][0-9A-Z]{5}|[A-Z][A-Z0-9.\-]{0,14})(?![A-Za-z0-9])", residual, re.IGNORECASE)
        code = code_match.group(0) if code_match else ""
        if code_match:
            masked[code_match.start():code_match.end()] = " " * len(code)
        residual_name = re.sub(r"[()[\]{},;:]+", " ", "".join(masked)).strip()
        raw = {"date": date_match.group(0) if date_match else effective_date,
               "account": account_match.group(1) if account_match else (bracket_account.group(1) if bracket_account else default_account),
               "code": code, "name": " ".join(residual_name.split()), "side": side_match.group(0),
               "quantity": quantity_match.group(1) if quantity_match else "",
               "price": (price_match.group(1) or price_match.group(2)) if price_match else "",
               "memo": original}
        if price_match:
            if "$" in price_match.group(0) or (price_match.group(3) or "").upper() in {"USD", "달러"}:
                raw["currency"] = "USD"
            elif "₩" in price_match.group(0) or (price_match.group(3) or "").upper() in {"KRW", "원"}:
                raw["currency"] = "KRW"
        if id_match:
            raw["external_id"] = id_match.group(1)
        warnings = []
        if not date_match:
            warnings.append(f"원문에 날짜가 없어 선택한 기준일 {effective_date}을 사용했습니다.")
        if not account_match and not bracket_account and default_account:
            warnings.append(f"선택한 기본 계좌 '{default_account}'를 사용했습니다.")
        builder.trade(raw, "text", row_number, extra_errors=parse_errors, extra_warnings=warnings)
    if not text.strip():
        builder.preview.issues.append("가져올 텍스트가 비어 있습니다.")
    return builder.preview
