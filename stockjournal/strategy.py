"""Read-only 200티큐 decision support; never submits or invents executions.

The supplied May 7, 2026 article defines the strategy. Explicit implementation
choices and data limitations are documented in docs/strategy-200tqqq.md.
All monetary calculations use Decimal in a private precision-50 context.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP, localcontext
import hashlib
from itertools import chain
import json
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

from .domain import decimal_value, portfolio, validate_state
from . import market_calendar


FIELD = "strategy_200tqqq"
RULESET_ID = "200tqqq-pdf-20260507-v1"
SOURCE_URL = "https://www.fmkorea.com/9797763685"
ZERO = Decimal(0)
NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Action:
    id: str
    kind: str
    symbol: str
    quantity: Decimal | None
    budget_usd: Decimal | None
    reason: str
    threshold_pct: Decimal | None = None
    entry_number: int | None = None


@dataclass
class Decision:
    account_id: str
    status: str = "hold"
    title: str = "조건 확인 대기"
    as_of: str = ""
    history_hash: str = ""
    close: Decimal | None = None
    sma: Decimal | None = None
    previous_close: Decimal | None = None
    previous_sma: Decimal | None = None
    average_cost: Decimal = ZERO
    usd_return_pct: Decimal | None = None
    broker_return_pct: Decimal | None = None
    cash_usd: Decimal = ZERO
    quantity_step: Decimal = Decimal(1)
    cycle: dict | None = None
    positions: dict = field(default_factory=dict)
    actions: list[Action] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    manual_review: bool = False
    existing_trade_ids: tuple[str, ...] = ()
    evaluated_at: str = ""


def _snapshot_value(value):
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("계획 스냅샷에 유한하지 않은 숫자가 있습니다.")
        return format(value, "f")
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("계획 스냅샷의 필드 이름은 문자열이어야 합니다.")
        return {key: _snapshot_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_snapshot_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    raise ValueError("계획 스냅샷에는 정확한 소수 문자열을 사용해야 합니다.")


def decision_to_dict(decision):
    """Freeze a Decision into plain JSON data without recalculating its intent."""
    if not isinstance(decision, Decision):
        raise ValueError("저장할 전략 계획을 확인하세요.")
    return _snapshot_value(asdict(decision))


def decision_from_dict(payload):
    """Restore the exact saved snapshot; does not fetch data or make new plans."""
    if not isinstance(payload, dict):
        raise ValueError("전략 계획 스냅샷은 객체여야 합니다.")
    data = _snapshot_value(payload)
    known = {item.name for item in fields(Decision)}
    if set(data) != known:
        raise ValueError("전략 계획 스냅샷의 필드가 빠졌거나 지원하지 않는 필드가 있습니다.")

    def exact(value, *, signed=False):
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("계획의 금액·수량은 정확한 소수 문자열이어야 합니다.")
        if signed:
            if len(value) > 128 or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value):
                raise ValueError("저장된 수익률 형식이 올바르지 않습니다.")
            result = Decimal(value)
            if not result.is_finite():
                raise ValueError("저장된 수익률은 유한해야 합니다.")
            return result
        return decimal_value(value, "저장된 계획 숫자")

    for name in ("close", "sma", "previous_close", "previous_sma", "average_cost", "cash_usd", "quantity_step"):
        data[name] = exact(data[name])
    for name in ("usd_return_pct", "broker_return_pct"):
        data[name] = exact(data[name], signed=True)
    if data["cash_usd"] is None or data["quantity_step"] is None or data["quantity_step"] <= 0 or data["average_cost"] is None:
        raise ValueError("계획의 현금·수량 단위·원가를 확인하세요.")
    if not isinstance(data["actions"], list) or not isinstance(data["positions"], dict):
        raise ValueError("계획의 행동·보유분 목록을 확인하세요.")
    actions = []
    action_fields = {item.name for item in fields(Action)}
    for action in data["actions"]:
        if not isinstance(action, dict) or set(action) != action_fields:
            raise ValueError("계획 행동 스냅샷의 필드를 확인하세요.")
        if action["kind"] not in ("BUY_ENTRY", "SELL_PROFIT", "EXIT", "BUY_SPYM") or action["symbol"] not in ("TQQQ", "SPYM", "SGOV"):
            raise ValueError("지원하지 않는 계획 행동입니다.")
        for name in ("quantity", "budget_usd", "threshold_pct"):
            action[name] = exact(action[name])
        if action["entry_number"] is not None and (type(action["entry_number"]) is not int or not 1 <= action["entry_number"] <= 3):
            raise ValueError("저장된 분할 매수 차수가 올바르지 않습니다.")
        if not isinstance(action["id"], str) or not action["id"] or not isinstance(action["reason"], str):
            raise ValueError("저장된 계획 행동의 식별자와 근거를 확인하세요.")
        actions.append(Action(**action))
    data["actions"] = actions
    for position in data["positions"].values():
        if not isinstance(position, dict) or set(position) != {"quantity", "cost"}:
            raise ValueError("계획의 보유분 형식을 확인하세요.")
        position["quantity"], position["cost"] = exact(position["quantity"]), exact(position["cost"])
        if position["quantity"] is None or position["cost"] is None:
            raise ValueError("계획의 보유 수량과 원가가 필요합니다.")
    for name in ("warnings", "blockers", "existing_trade_ids"):
        if not isinstance(data[name], list) or any(not isinstance(item, str) for item in data[name]):
            raise ValueError("계획의 검증 정보 형식을 확인하세요.")
    data["existing_trade_ids"] = tuple(data["existing_trade_ids"])
    if type(data["manual_review"]) is not bool or not isinstance(data["account_id"], str) or not data["account_id"]:
        raise ValueError("저장된 계획의 계좌·검토 상태를 확인하세요.")
    if data["cycle"] is not None and not isinstance(data["cycle"], dict):
        raise ValueError("저장된 사이클 형식이 올바르지 않습니다.")
    return Decision(**data)


def _now(value=None):
    result = value or datetime.now(timezone.utc)
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError("현재 시각에는 시간대가 필요합니다.")
    return result.astimezone(timezone.utc)


def completed_session_date(now=None):
    """Published exchange session close plus a ten-minute finalization buffer."""
    return date.fromisoformat(market_calendar.latest_completed(_now(now)))


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다.")
    return date.fromisoformat(value)


def _signed_decimal(value):
    if isinstance(value, (bool, float)):
        raise ValueError("수익률은 정확한 소수 문자열로 입력하세요.")
    raw = str(value)
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", raw) or len(raw) > 128:
        raise ValueError("수익률은 유한한 숫자로 입력하세요.")
    value = Decimal(raw)
    if not value.is_finite() or abs(value) > Decimal("1000000"):
        raise ValueError("입력 수익률의 범위를 확인하세요.")
    return value


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _config(state, account_id):
    account = state["accounts"].get(account_id)
    if not account or account.get("deleted"):
        raise ValueError("200티큐를 확인할 계좌 하나를 선택하세요.")
    config = copy.deepcopy(account.get(FIELD, {}))
    if not isinstance(config, dict) or config.get("version", 1) != 1:
        raise ValueError("200티큐 설정 형식을 확인하세요.")
    config.setdefault("version", 1)
    config.setdefault("cash_usd", "0")
    config.setdefault("quantity_step", "1")
    config.setdefault("cycle", None)
    config.setdefault("confirmations", [])
    cycle = config["cycle"]
    if cycle is not None:
        if not isinstance(cycle, dict) or not isinstance(cycle.get("id"), str) or not cycle["id"]:
            raise ValueError("200티큐 사이클 식별자가 올바르지 않습니다.")
        if type(cycle.get("entry_count")) is not int or not 1 <= cycle["entry_count"] <= 3:
            raise ValueError("200티큐 분할 매수 차수를 확인하세요.")
        _date(cycle["start_date"])
        _date(cycle["last_entry_date"])
        if not isinstance(cycle.get("profit_levels"), list):
            raise ValueError("200티큐 익절 단계 형식을 확인하세요.")
        for level in cycle["profit_levels"]:
            _profit_level(level)
    if not isinstance(config["confirmations"], list):
        raise ValueError("200티큐 체결 확인 기록을 확인하세요.")
    return config


def _profit_level(value):
    result = decimal_value(value, "익절 단계", positive=True)
    if result not in (Decimal(10), Decimal(25), Decimal(50)) and (result < 100 or result % 100):
        raise ValueError("익절 단계는 10, 25, 50, 100, 200, 300…입니다.")
    return result


def _positions(state, account_id):
    result = {symbol: {"quantity": ZERO, "cost": ZERO} for symbol in ("TQQQ", "SPYM", "SGOV")}
    for row in portfolio(state, account_id=account_id)["rows"]:
        instrument = state["instruments"][row["instrument_id"]]
        symbol = instrument["code"].upper()
        if symbol in result and instrument["currency"] == "USD":
            result[symbol]["quantity"] += row["quantity"]
            result[symbol]["cost"] += row["cost"]
    return result


def _holding_hash(state, account_id):
    rows = []
    for row in state["trades"].values():
        if row.get("account_id") == account_id and not row.get("deleted"):
            instrument = state["instruments"][row["instrument_id"]]
            if instrument["code"].upper() == "TQQQ" and instrument["currency"] == "USD":
                rows.append(row)
    return _hash(sorted(rows, key=lambda row: row["id"]))


def _history(history):
    if not isinstance(history, dict) or history.get("symbol") != "TQQQ" or history.get("currency") != "USD":
        raise ValueError("TQQQ · USD 종가 자료가 필요합니다.")
    if history.get("session") != "regular":
        raise ValueError("미국 정규장 종가 자료만 사용할 수 있습니다.")
    raw = history.get("rows")
    if not isinstance(raw, list) or len(raw) > 10000:
        raise ValueError("종가 자료 형식이나 행 수를 확인하세요.")
    rows, seen = [], set()
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("종가 행 형식이 올바르지 않습니다.")
        day = _date(row.get("date"))
        if day.isoformat() in seen:
            raise ValueError("종가 자료에 같은 거래일이 중복되었습니다.")
        if day.weekday() >= 5:
            raise ValueError("주말 날짜의 정규장 종가를 확인하세요.")
        seen.add(day.isoformat())
        close = decimal_value(row.get("close"), "정규장 종가", positive=True, string_only=False)
        rows.append((day, close))
    rows.sort()
    digest = _hash({"rows": [[day.isoformat(), format(close, "f")] for day, close in rows],
                    "symbol": "TQQQ", "currency": "USD", "session": "regular",
                    "adjustment": history.get("adjustment"), "finalization": history.get("finalization")})
    return rows, digest


def _history_checks(history, config, now):
    warnings, blockers = [], []
    rows, digest = _history(history)
    verified = config.get("history_confirmation", {}).get("hash") == digest
    if len(rows) < 201:
        blockers.append("상향 돌파 판정에는 서로 다른 정규장 종가 201개 이상이 필요합니다.")
    cutoff = completed_session_date(now)
    if rows:
        last = rows[-1][0]
        if last > cutoff:
            blockers.append("장중·미래 날짜의 종가입니다. 거래소 정규장 종료 10분 이후의 완료 종가를 사용하세요.")
        elif last < cutoff:
            blockers.append(f"최신 종가 확인 필요: 자료 {last}, 거래소 달력의 최근 완료일 {cutoff}. 오래된 종가의 확인만으로 신호를 만들지 않습니다.")
        if any((right[0] - left[0]).days > 7 for left, right in zip(rows, rows[1:])):
            blockers.append("종가 자료에 7일을 넘는 공백이 있습니다. 누락된 거래일을 보완하세요.")
    if history.get("finalization") not in ("completed_sessions", "historical_dates_only", "conservative_regular_close", "user_verified"):
        blockers.append("종가의 정규장 완료 여부가 확인되지 않았습니다.")
    if history.get("adjustment") not in ("split_adjusted", "split-adjusted"):
        if not verified:
            blockers.append("분할 보정 여부가 불명확합니다. 보정 종가 CSV 또는 현재 자료의 분할·최신일 확인이 필요합니다.")
        warnings.append("원본 가격 제공자가 주식 분할 보정을 보장하지 않습니다. 사용자 확인은 가격을 보정하지 않습니다.")
    if rows and any(right[1] / left[1] > Decimal("1.8") or right[1] / left[1] < Decimal("0.55") for left, right in zip(rows, rows[1:])):
        if not verified:
            blockers.append("큰 가격 단절이 있습니다. 분할·병합과 데이터 오류를 확인하세요.")
        warnings.append("큰 일간 가격 변화가 있어 분할 보정 및 실제 시장 변동을 별도로 확인해야 합니다.")
    warnings.append("NYSE 공시 2026~2028 휴장·조기폐장 일정을 사용하며 뉴욕 시각과 서머타임을 반영합니다. 예고 없는 휴장과 실제 주문 가능 여부는 증권사 확인이 필요합니다.")
    for warning in history.get("warnings", []):
        if isinstance(warning, str) and warning not in warnings:
            warnings.append(warning)
    return rows, digest, warnings, blockers


def evaluate(state, account_id, history, *, cash_usd=None, quantity_step=None, broker_return_pct=None, now=None):
    """Calculate a reviewable plan without changing state or invoking a broker."""
    with localcontext() as context:
        context.prec = 50
        return _evaluate(state, account_id, history, cash_usd=cash_usd, quantity_step=quantity_step,
                         broker_return_pct=broker_return_pct, now=_now(now))


def _evaluate(state, account_id, history, *, cash_usd, quantity_step, broker_return_pct, now):
    validate_state(state)
    config = _config(state, account_id)
    cash = decimal_value(config["cash_usd"] if cash_usd is None else cash_usd, "분할 매수용 현금", string_only=False)
    step = decimal_value(config["quantity_step"] if quantity_step is None else quantity_step, "매매 수량 단위", positive=True, string_only=False)
    result = Decision(account_id=account_id, cash_usd=cash, quantity_step=step, cycle=copy.deepcopy(config["cycle"]),
                      positions=_positions(state, account_id), existing_trade_ids=tuple(state["trades"]), evaluated_at=now.isoformat())
    try:
        rows, result.history_hash, result.warnings, result.blockers = _history_checks(history, config, now)
    except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
        result.status, result.title = "blocked", "가격 자료를 확인하세요"
        result.blockers.append(str(exc))
        return result
    if rows:
        result.as_of, result.close = rows[-1][0].isoformat(), rows[-1][1]
    if len(rows) >= 201:
        result.sma = sum((close for _, close in rows[-200:]), ZERO) / 200
        result.previous_sma = sum((close for _, close in rows[-201:-1]), ZERO) / 200
        result.previous_close = rows[-2][1]
    holding = result.positions["TQQQ"]
    if holding["quantity"]:
        result.average_cost = holding["cost"] / holding["quantity"]
        if result.average_cost and result.close:
            result.usd_return_pct = (result.close / result.average_cost - 1) * 100
    if broker_return_pct is not None and str(broker_return_pct).strip():
        result.broker_return_pct = _signed_decimal(broker_return_pct)
    elif config.get("broker_return"):
        saved = config["broker_return"]
        if saved.get("date") == result.as_of and saved.get("holdings_hash") == _holding_hash(state, account_id):
            result.broker_return_pct = _signed_decimal(saved["pct"])
        else:
            result.warnings.append("저장한 증권사 수익률의 종가 날짜 또는 TQQQ 기록이 달라졌습니다. 수익률을 다시 입력하세요.")
    result.warnings.append("USD 원가 수익률은 수수료·세금을 포함한 참고값입니다. 원문 익절 기준은 환차손을 포함한 계좌 수익률입니다.")
    if result.blockers:
        result.status, result.title = "blocked", "자료 확인 전 신호 보류"
        return result
    cycle = config["cycle"]
    cycle_key = cycle["id"] if cycle else "new"

    def add(kind, symbol, quantity, budget, reason, threshold=None, entry=None):
        identity = f"{RULESET_ID}:{account_id}:{cycle_key}:{result.as_of}:{kind}:{symbol}:{threshold or entry or ''}"
        if kind == "BUY_SPYM":
            # A confirmed partial purchase leaves a distinct remaining-cash
            # intent, while repeatedly evaluating the same state stays stable.
            identity += ":" + _hash(config["confirmations"])
        result.actions.append(Action(_hash(identity)[:24], kind, symbol, quantity, budget, reason, threshold, entry))

    if result.close < result.sma:
        result.status, result.title = "exit", "200일선 아래: TQQQ·SPYM 전량 정리 검토"
        for symbol in ("TQQQ", "SPYM"):
            quantity = result.positions[symbol]["quantity"]
            if quantity:
                add("EXIT", symbol, quantity, None, "분할 매수 중에도 전량 정리. 매도대금은 SGOV로 이동.")
        if not result.actions:
            result.status, result.title = "cash", "200일선 아래: SGOV 대기"
        result.warnings.append("TQQQ와 SPYM 매도대금을 SGOV로 이동하는 단계는 실제 체결 후 별도로 기록하세요.")
        return result
    if result.close == result.sma:
        result.status, result.title = "hold", "종가 = 200일선: 보류"
        result.warnings.append("선과 같은 가격은 매수·매도 신호를 내지 않는 구현 가정입니다.")
        return result
    if config.get("costs_review_required"):
        result.status, result.title = "blocked", "체결 비용과 남은 예산 확인이 필요합니다"
        result.manual_review = True
        result.blockers.append(config.get("costs_review_note") or "실제 체결 수수료·세금이 확정되지 않았습니다. 분할 매수용 재원과 SPYM 대기금을 확인하고 비용 확인을 선택해 저장하세요.")
        return result
    if cycle is None and (holding["quantity"] or result.positions["SPYM"]["quantity"]):
        result.status, result.title = "untracked", "기존 보유분의 사이클을 먼저 연결하세요"
        result.warnings.append("실제 보유분은 있으나 분할 매수·이전 익절 단계가 없습니다. 기존 사이클을 수동 연결해야 중복 익절을 피할 수 있습니다.")
        return result
    entry_number = None
    if cycle is None:
        last_exit = config.get("last_exit_date", "")
        if result.previous_close <= result.previous_sma and result.as_of > last_exit:
            entry_number = 1
    elif cycle["entry_count"] < 3 and result.as_of > cycle["last_entry_date"]:
        following = [day.isoformat() for day, _ in rows if day.isoformat() > cycle["last_entry_date"]]
        if following and following[0] == result.as_of:
            entry_number = cycle["entry_count"] + 1
        else:
            result.warnings.append("다음 거래일 분할 매수 시점을 지났습니다. 놓친 차수를 자동으로 합치지 않습니다. 실제 이력을 확인해 사이클을 다시 연결하세요.")
            result.manual_review = True
    if entry_number:
        if config.get("reserve_review_required"):
            result.status, result.title = "blocked", "다음 진입 전에 실제 순매도대금과 예산을 확인하세요"
            result.manual_review = True
            result.blockers.append(config.get("reserve_review_note") or "이전 전량 매도의 실제 수수료·세금을 확인하지 못했습니다. 가용 재원을 확인하고 분할 매수용 현금을 다시 저장하세요.")
            return result
        budget = cash / (4 - entry_number)
        quantity = (budget / result.close / step).to_integral_value(rounding=ROUND_FLOOR) * step
        add("BUY_ENTRY", "TQQQ", quantity, budget, f"{entry_number}/3차: 입력한 남은 매수 예산의 {['1/3', '1/2', '전부'][entry_number-1]}. 실제 체결·수수료 확인 필요.", entry=entry_number)
        result.status, result.title = "entry", f"200일선 위: {entry_number}차 분할 매수 검토"
        if quantity == 0:
            result.manual_review = True
            result.warnings.append("매수 예산이 수량 단위보다 작습니다. SGOV 환매 등 실제 현금 확보 후 예산을 다시 입력하세요.")
    else:
        result.status, result.title = "hold", "200일선 위: 보유 / 다음 조건 대기"
    if cycle and holding["quantity"]:
        if result.broker_return_pct is None:
            result.warnings.append("익절 검토에는 같은 종가 날짜의 환차손 포함 증권사 TQQQ 수익률(%)이 필요합니다.")
        else:
            completed = {Decimal(level) for level in cycle["profit_levels"]}
            remaining = holding["quantity"]
            thresholds = chain((10, 25, 50), range(100, max(100, int(result.broker_return_pct // 100) * 100 + 1), 100))
            for raw in thresholds:
                threshold = Decimal(raw)
                if remaining <= 0 or threshold > result.broker_return_pct:
                    break
                if threshold in completed:
                    continue
                fraction = Decimal("0.1") if threshold < 100 else Decimal("0.5")
                quantity = min(remaining, (remaining * fraction / step).to_integral_value(rounding=ROUND_HALF_UP) * step)
                add("SELL_PROFIT", "TQQQ", quantity, None, f"+{threshold}% 최초 도달: 당시 남은 수량의 {fraction * 100}%를 검토. 순매도대금은 SPYM.", threshold=threshold)
                remaining -= quantity
            profits = [action for action in result.actions if action.kind == "SELL_PROFIT"]
            if profits:
                result.status, result.title = "profit", "환차손 포함 계좌 수익률: 익절 단계 검토"
            if len(profits) > 1:
                result.manual_review = True
                result.warnings.append("여러 임계값을 한 번에 넘었습니다. 표는 낮은 단계부터 순차 적용한 시나리오이며, 원문은 갭 통과 처리를 명시하지 않습니다. 실제 실행 범위를 직접 결정하세요.")
            if profits and entry_number:
                result.manual_review = True
                result.warnings.append("분할 매수와 익절 조건이 동시에 있습니다. 같은 날의 처리 순서는 원문에 없어 자동 실행을 보류합니다.")
            if any(action.quantity == 0 for action in profits):
                result.manual_review = True
                result.warnings.append("반올림 결과가 0주인 단계가 있습니다. 자동으로 1주를 매도하거나 실행 완료로 표시하지 않습니다.")
        pending = decimal_value(cycle.get("pending_spym_usd", "0"), "SPYM 대기금")
        if pending:
            add("BUY_SPYM", "SPYM", None, pending, "확인된 TQQQ 익절 순매도대금. 이 사이클에서 TQQQ로 되돌리지 않습니다.")
    return result


def _save_config(state, account_id, config, now):
    target = copy.deepcopy(state)
    target["accounts"][account_id].update({FIELD: config, "updated_at": _now(now).isoformat(timespec="microseconds")})
    validate_state(target)
    return target


def confirm_history(state, account_id, history, *, now=None):
    """Acknowledge this exact series; acknowledgement never adjusts prices."""
    now = _now(now)
    validate_state(state)
    config = _config(state, account_id)
    with localcontext() as context:
        context.prec = 50
        rows, digest = _history(history)
        if len(rows) < 201 or rows[-1][0] != completed_session_date(now):
            raise ValueError("201개 이상이며 최근 완료 정규장에 해당하는 종가만 확인할 수 있습니다.")
        config["history_confirmation"] = {"hash": digest, "confirmed_at": now.isoformat(), "latest_date": rows[-1][0].isoformat()}
        _, _, _, blockers = _history_checks(history, config, now)
        if blockers:
            raise ValueError("\n".join(blockers))
    return _save_config(state, account_id, config, now)


def update_preferences(state, account_id, *, cash_usd, quantity_step="1", broker_return_pct=None, return_date=None,
                       pending_spym_usd=None, costs_reviewed=False, now=None):
    validate_state(state)
    config = _config(state, account_id)
    config["cash_usd"] = format(decimal_value(cash_usd, "분할 매수용 현금", string_only=False), "f")
    config["quantity_step"] = format(decimal_value(quantity_step, "수량 단위", positive=True, string_only=False), "f")
    if type(costs_reviewed) is not bool:
        raise ValueError("체결 비용 확인 여부는 명시적으로 선택해야 합니다.")
    if pending_spym_usd is not None:
        if config["cycle"] is None:
            raise ValueError("SPYM 대기금은 진행 중인 사이클에서만 입력할 수 있습니다.")
        config["cycle"]["pending_spym_usd"] = format(decimal_value(pending_spym_usd, "확인한 SPYM 대기금", string_only=False), "f")
    if costs_reviewed:
        if config["cycle"] is not None and (config.get("costs_review_required") or config.get("reserve_review_required")) and pending_spym_usd is None:
            raise ValueError("체결 비용을 확인할 때 실제 남은 SPYM 대기금도 명시하세요.")
        for key in ("reserve_review_required", "reserve_review_note", "costs_review_required", "costs_review_note"):
            config.pop(key, None)
    if broker_return_pct is not None and str(broker_return_pct).strip():
        if not return_date:
            raise ValueError("증권사 수익률과 일치하는 미국 종가 날짜가 필요합니다.")
        config["broker_return"] = {"pct": format(_signed_decimal(broker_return_pct), "f"), "date": _date(return_date).isoformat(),
                                    "holdings_hash": _holding_hash(state, account_id)}
    else:
        config.pop("broker_return", None)
    return _save_config(state, account_id, config, now)


def adopt_cycle(state, account_id, *, start_date, entry_count, profit_levels=(), last_entry_date=None, now=None):
    """Attach user-verified existing holdings; does not fabricate any fills."""
    now = _now(now)
    validate_state(state)
    config = _config(state, account_id)
    if type(entry_count) is not int or not 1 <= entry_count <= 3:
        raise ValueError("완료한 분할 매수 차수는 1~3입니다.")
    start = _date(start_date)
    last = _date(last_entry_date or start_date)
    if start > last or last > now.astimezone(NY).date():
        raise ValueError("사이클 시작일과 마지막 매수일을 확인하세요.")
    with localcontext() as context:
        context.prec = 50
        if not _positions(state, account_id)["TQQQ"]["quantity"]:
            raise ValueError("실제 원장에 TQQQ 보유분이 있어야 연결할 수 있습니다.")
        levels = sorted({_profit_level(str(level)) for level in profit_levels})
    config["cycle"] = {"id": str(uuid4()), "start_date": start.isoformat(), "last_entry_date": last.isoformat(),
                       "entry_count": entry_count, "profit_levels": [format(level, "f") for level in levels],
                       "pending_spym_usd": (config["cycle"] or {}).get("pending_spym_usd", "0"),
                       "adopted": True, "adopted_at": now.isoformat()}
    return _save_config(state, account_id, config, now)


def _trade_day(value):
    if "T" not in value:
        return _date(value)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(NY).date() if parsed.tzinfo is not None else parsed.date()


def confirm_action(state, account_id, decision, action_id, trade_ids, *, execution_evidence=None, now=None):
    """Mark a reviewed action only from actual, unused journal executions."""
    with localcontext() as context:
        context.prec = 50
        return _confirm_action(state, account_id, decision, action_id, trade_ids, _now(now), execution_evidence)


def _confirm_action(state, account_id, decision, action_id, trade_ids, now, execution_evidence):
    validate_state(state)
    config = _config(state, account_id)
    if decision.account_id != account_id or decision.blockers:
        raise ValueError("이 계좌의 유효한 계획이 아닙니다.")
    action = next((item for item in decision.actions if item.id == action_id), None)
    if action is None or action.quantity == 0:
        raise ValueError("실행할 수량이 있는 계획을 선택하세요.")
    ids = list(trade_ids)
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("서로 다른 실제 체결 기록을 선택하세요.")
    used = {uid for item in config["confirmations"] for uid in item.get("trade_ids", [])}
    if any(item.get("action_id") == action_id for item in config["confirmations"]) or used.intersection(ids):
        raise ValueError("이미 확인한 계획 또는 체결 기록입니다.")
    cycle = config["cycle"]
    expected_cycle = decision.cycle["id"] if decision.cycle else None
    if (cycle["id"] if cycle else None) != expected_cycle:
        raise ValueError("계획을 만든 뒤 사이클이 변경되었습니다. 새 계획을 확인하세요.")
    selected = []
    side = "BUY" if action.kind in ("BUY_ENTRY", "BUY_SPYM") else "SELL"
    signal_session = market_calendar.session(decision.as_of)
    if signal_session is None:
        raise ValueError("신호 날짜가 거래소 정규 거래일이 아닙니다.")
    if execution_evidence is not None and not isinstance(execution_evidence, dict):
        raise ValueError("실제 주문 관찰 근거는 체결 ID별 객체여야 합니다.")
    for uid in ids:
        row = state["trades"].get(uid)
        if not row or row.get("deleted") or row.get("account_id") != account_id or row.get("side") != side:
            raise ValueError("선택한 계좌·매매 구분과 실제 체결 기록이 다릅니다.")
        instrument = state["instruments"][row["instrument_id"]]
        if instrument["code"].upper() != action.symbol or row["currency"] != "USD":
            raise ValueError("계획 종목과 다른 실제 체결 기록입니다.")
        day = _trade_day(row["executed_at"])
        window_verified = False
        if execution_evidence is not None and uid in execution_evidence:
            evidence = execution_evidence[uid]
            if (not isinstance(evidence, dict) or set(evidence) != {"submitted_at", "observed_at"}
                    or row.get("source") != "NH" or row.get("external_id") != uid or not uid.startswith("nh-us:")):
                raise ValueError("검증한 NH 주문·실제 체결에만 관찰 시각 근거를 사용할 수 있습니다.")
            try:
                submitted = datetime.fromisoformat(evidence["submitted_at"].replace("Z", "+00:00"))
                observed = datetime.fromisoformat(evidence["observed_at"].replace("Z", "+00:00"))
                if submitted.tzinfo is None or observed.tzinfo is None:
                    raise ValueError
                if not signal_session["close"] <= submitted <= observed <= now:
                    raise ValueError
            except (AttributeError, TypeError, ValueError):
                raise ValueError("신호 마감 이후 주문 제출·실체결 관찰·현재 시각 순서를 확인하세요.") from None
            candidate_dates = {submitted.astimezone(NY).date(), submitted.astimezone(ZoneInfo("Asia/Seoul")).date()}
            if day not in candidate_dates:
                raise ValueError("NH 업무일이 확인된 주문 제출의 미국·한국 날짜와 다릅니다.")
            window_verified = True
        if day < _date(decision.as_of) or (day > now.astimezone(NY).date() and not window_verified) or uid in decision.existing_trade_ids:
            raise ValueError("계획 전 기록·미래 기록은 실행 확인에 재사용할 수 없습니다.")
        if "T" in row["executed_at"]:
            stamp = datetime.fromisoformat(row["executed_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is not None and stamp > now:
                raise ValueError("미래 시각의 체결은 실행 확인에 사용할 수 없습니다.")
            if stamp.tzinfo is not None and stamp < signal_session["close"]:
                raise ValueError("신호가 나온 정규장 종료 전 체결은 이 계획의 실행으로 확인할 수 없습니다.")
        selected.append(row)
    quantity = sum((Decimal(row["quantity"]) for row in selected), ZERO)
    gross = sum((Decimal(row["quantity"]) * Decimal(row["price"]) for row in selected), ZERO)
    charges = sum((Decimal(row["fee"]) + Decimal(row["tax"]) for row in selected), ZERO)
    if action.kind == "BUY_ENTRY":
        if cycle and action.entry_number != cycle["entry_count"] + 1 or not cycle and action.entry_number != 1:
            raise ValueError("분할 매수 차수가 이미 진행되었거나 순서가 다릅니다.")
        if gross + charges > action.budget_usd:
            raise ValueError("체결 비용이 이 차수의 예산을 넘습니다. 이후 차수의 예산을 사용하지 않도록 실제 기록을 재검토하세요.")
        if cycle is None:
            cycle = {"id": str(uuid4()), "start_date": decision.as_of, "entry_count": 0, "profit_levels": [], "pending_spym_usd": "0"}
            config["cycle"] = cycle
        cycle.update(entry_count=action.entry_number, last_entry_date=decision.as_of)
        config["cash_usd"] = format(decision.cash_usd - gross - charges, "f")
    elif action.kind == "SELL_PROFIT":
        if cycle is None or quantity != action.quantity or format(action.threshold_pct, "f") in cycle["profit_levels"]:
            raise ValueError("실제 매도 수량 또는 이미 완료한 익절 단계를 확인하세요.")
        lower = [item.threshold_pct for item in decision.actions if item.kind == "SELL_PROFIT" and item.threshold_pct < action.threshold_pct and item.quantity]
        if any(format(level, "f") not in cycle["profit_levels"] for level in lower):
            raise ValueError("여러 단계 시나리오는 낮은 익절 단계의 실제 체결부터 확인하세요.")
        if gross < charges:
            raise ValueError("매도 비용이 대금보다 큽니다. 실제 체결 비용을 확인하세요.")
        cycle["profit_levels"] = sorted([*cycle["profit_levels"], format(action.threshold_pct, "f")], key=Decimal)
        cycle["pending_spym_usd"] = format(Decimal(cycle.get("pending_spym_usd", "0")) + gross - charges, "f")
    elif action.kind == "BUY_SPYM":
        if cycle is None or gross + charges > Decimal(cycle.get("pending_spym_usd", "0")):
            raise ValueError("SPYM 매수 비용과 확인된 익절 대금을 확인하세요.")
        cycle["pending_spym_usd"] = format(Decimal(cycle["pending_spym_usd"]) - gross - charges, "f")
    elif action.kind == "EXIT":
        if quantity != action.quantity or _positions(state, account_id)[action.symbol]["quantity"]:
            raise ValueError("전량 정리는 계획한 전량의 실제 매도와 원장 잔고 0을 확인해야 합니다.")
        positions = _positions(state, account_id)
        exit_ids = {item.id for item in decision.actions if item.kind == "EXIT"}
        confirmed_ids = {item.get("action_id") for item in config["confirmations"]} | {action_id}
        if not positions["TQQQ"]["quantity"] and not positions["SPYM"]["quantity"] and exit_ids <= confirmed_ids:
            config["cycle"] = None
            config["last_exit_date"] = decision.as_of
    else:
        raise ValueError("지원하지 않는 계획 확인입니다.")
    config["confirmations"].append({"action_id": action_id, "kind": action.kind, "signal_date": decision.as_of,
                                    "trade_ids": ids, "confirmed_at": now.isoformat(), "quantity": format(quantity, "f")})
    config.pop("broker_return", None)
    return _save_config(state, account_id, config, now)


def reset_cycle(state, account_id, *, now=None):
    validate_state(state)
    config = _config(state, account_id)
    with localcontext() as context:
        context.prec = 50
        positions = _positions(state, account_id)
        if positions["TQQQ"]["quantity"] or positions["SPYM"]["quantity"]:
            raise ValueError("TQQQ와 SPYM 모두 실제 잔고가 0이어야 사이클을 종료할 수 있습니다.")
    config["cycle"] = None
    config["last_exit_date"] = completed_session_date(now).isoformat()
    config.pop("broker_return", None)
    return _save_config(state, account_id, config, now)
