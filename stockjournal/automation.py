"""Durable, local, fail-closed execution of an already evaluated strategy.

No broker call is made at import. A broker implementation is injected and live
execution additionally needs an in-memory arm for one account. NH has no server
idempotency key: a committed INTENT with an uncertain result is never resent.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import platform
import re
import sqlite3
import threading
from typing import Any, Callable, Protocol
import uuid


ZERO = Decimal(0)
SYMBOLS = ("TQQQ", "SPYM", "SGOV")
SUCCESS = {"FILLED", "SIMULATED", "SKIPPED"}
TERMINAL = {"COMPLETE", "SIMULATED", "NO_ACTION", "ABORTED"}
UNCERTAIN = {"INTENT", "SUBMITTED", "PARTIAL", "UNKNOWN", "CANCEL_REQUESTED"}


class AutomationBlocked(ValueError):
    """The next write cannot be justified from the available evidence."""


class OwnershipError(AutomationBlocked):
    pass


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: Decimal
    captured_at: datetime
    currency: str = "USD"


@dataclass(frozen=True)
class BrokerSnapshot:
    account_id: str
    captured_at: datetime
    holdings: dict[str, Decimal]
    available_usd: Decimal
    pending_orders: tuple[str, ...] = ()
    market_open: bool = False
    market_close_at: datetime | None = None
    data_verified: bool = False


@dataclass(frozen=True)
class OrderRequest:
    account_id: str
    symbol: str
    side: str
    quantity: int
    limit_price: Decimal
    intent_id: str


@dataclass(frozen=True)
class OrderUpdate:
    broker_order_id: str | None
    status: str
    filled_quantity: Decimal = ZERO
    average_price: Decimal | None = None
    updated_at: datetime | None = None
    cancel_requested: bool = False
    related_order_ids: tuple[str, ...] = ()
    message: str = ""
    commission_usd: Decimal | None = None
    tax_usd: Decimal | None = None


class BrokerExecutor(Protocol):
    account_id: str
    def snapshot(self, account_id: str, symbols: tuple[str, ...]) -> BrokerSnapshot: ...
    def quote(self, account_id: str, symbol: str) -> Quote: ...
    def submit_limit(self, request: OrderRequest) -> OrderUpdate: ...
    def order_status(self, account_id: str, broker_order_id: str) -> OrderUpdate: ...
    def cancel(self, account_id: str, broker_order_id: str) -> OrderUpdate: ...


@dataclass(frozen=True)
class SignalEvidence:
    account_id: str
    history_hash: str
    as_of: str
    source: str
    expected_session_date: str
    completed: bool
    adjustment_confirmed: bool
    latest_session_verified: bool
    verified_at: datetime
    broker_return_verified: bool = False
    entry_funding_from_sgov: bool = False
    exit_reserve_cash_usd: Decimal = ZERO


@dataclass(frozen=True)
class ExecutionPolicy:
    account_id: str
    broker_account_id: str
    max_order_usd: Decimal
    max_buy_usd: Decimal
    mode: str = "DRY_RUN"
    deadline_at: datetime | None = None
    fee_reserve_pct: Decimal = Decimal("0.005")
    buy_multiplier: Decimal = Decimal("1.001")
    sell_multiplier: Decimal = Decimal("0.999")
    quote_max_age_seconds: int = 60
    snapshot_max_age_seconds: int = 60
    signal_max_age_hours: int = 96
    evaluation_max_age_seconds: int = 300
    close_buffer_seconds: int = 30


@dataclass(frozen=True)
class AutomationContext:
    decision: Any
    evidence: SignalEvidence
    broker: BrokerExecutor


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AutomationBlocked("시간대가 확인된 시각이 필요합니다.")
    return value.astimezone(timezone.utc)


def _number(value, *, positive=False):
    if isinstance(value, (bool, float)):
        raise AutomationBlocked("금액·수량에는 정확한 소수 값이 필요합니다.")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise AutomationBlocked("금액·수량 형식을 확인하세요.") from exc
    if not result.is_finite() or result < 0 or (positive and not result) or result > Decimal("1000000000000"):
        raise AutomationBlocked("금액·수량의 허용 범위를 확인하세요.")
    return result


def _wire(value):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {str(key): _wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    return value


def _dump(value):
    return json.dumps(_wire(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _safe_message(value):
    return re.sub(r"\d{7,}", "[숫자 숨김]", " ".join(str(value).split()))[:350]


def machine_identity():
    """A machine-derived identity, deliberately outside the synchronized journal."""
    identity = platform.node() + ":" + str(uuid.getnode())
    if platform.system() == "Windows":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                identity = str(winreg.QueryValueEx(key, "MachineGuid")[0])
        except OSError:
            pass
    return hashlib.sha256(("stock-journal-execution:" + identity).encode()).hexdigest()


def decision_from_workflow(workflow):
    """Restore the pinned decision for confirmation against actual imported fills."""
    from .strategy import Action, Decision
    data = copy.deepcopy(workflow["decision"])
    for key in ("close", "sma", "previous_close", "previous_sma", "average_cost", "usd_return_pct",
                "broker_return_pct", "cash_usd", "quantity_step"):
        if data.get(key) is not None:
            data[key] = Decimal(data[key])
    for row in data.get("positions", {}).values():
        for key in ("quantity", "cost"):
            if key in row:
                row[key] = Decimal(row[key])
    actions = []
    for action in data.get("actions", []):
        for key in ("quantity", "budget_usd", "threshold_pct"):
            if action.get(key) is not None:
                action[key] = Decimal(action[key])
        actions.append(Action(**action))
    data["actions"] = actions
    data["existing_trade_ids"] = tuple(data.get("existing_trade_ids", ()))
    return Decision(**data)


class ExecutionJournal:
    """Separate FULL-synchronous SQLite store. Never include it in trade sync."""
    def __init__(self, path, machine_id=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.machine_id = machine_id or machine_identity()
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS execution_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_workflows (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_claims (
                    mode TEXT NOT NULL, broker_account_id TEXT NOT NULL, action_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL, PRIMARY KEY(mode, broker_account_id, action_id));
                CREATE TABLE IF NOT EXISTS execution_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL);
            """)
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute("SELECT value FROM execution_meta WHERE key='owner'").fetchone()
            if owner and owner[0] != self.machine_id:
                raise OwnershipError("다른 PC의 실행 상태입니다. 이 PC에서는 주문을 재개할 수 없습니다. 원래 PC와 NH 주문내역을 대조하세요.")
            connection.execute("INSERT OR IGNORE INTO execution_meta VALUES ('owner',?)", (self.machine_id,))

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=10)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=10000")
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, workflow_id):
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM execution_workflows WHERE id=?", (workflow_id,)).fetchone()
        if row is None:
            raise AutomationBlocked("실행 기록을 찾을 수 없습니다.")
        return json.loads(row[0])

    def list(self, limit=100):
        with self._connect() as connection:
            rows = connection.execute("SELECT payload FROM execution_workflows ORDER BY rowid DESC LIMIT ?", (int(limit),)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def events(self, workflow_id, limit=100):
        with self._connect() as connection:
            rows = connection.execute("SELECT created_at,event,detail FROM execution_events WHERE workflow_id=? ORDER BY sequence DESC LIMIT ?", (workflow_id, int(limit))).fetchall()
        return [{"created_at": row[0], "event": row[1], "detail": row[2]} for row in reversed(rows)]

    def _event(self, connection, workflow_id, event, detail):
        connection.execute("INSERT INTO execution_events(workflow_id,created_at,event,detail) VALUES (?,?,?,?)",
            (workflow_id, datetime.now(timezone.utc).isoformat(), event, _safe_message(detail)))

    def begin(self, workflow):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            old = connection.execute("SELECT payload FROM execution_workflows WHERE id=?", (workflow["id"],)).fetchone()
            if old:
                return json.loads(old[0])
            if workflow["mode"] == "LIVE":
                for row in connection.execute("SELECT payload FROM execution_workflows"):
                    existing = json.loads(row[0])
                    if existing["mode"] == "LIVE" and (existing["status"] not in TERMINAL or
                            existing["status"] == "COMPLETE" and not existing.get("applied")):
                        raise AutomationBlocked("이 PC에 미정리 실행 또는 원장 반영 대기 기록이 있습니다. 먼저 상태를 대조하세요.")
            for action in workflow["decision"]["actions"] if workflow["mode"] == "LIVE" else ():
                claimed = connection.execute("SELECT workflow_id FROM execution_claims WHERE mode=? AND broker_account_id=? AND action_id=?",
                    (workflow["mode"], workflow["broker_account_id"], action["id"])).fetchone()
                if claimed:
                    raise AutomationBlocked("이미 실행 기록이 있는 전략 단계입니다. 자동 재주문하지 않습니다.")
                connection.execute("INSERT INTO execution_claims VALUES (?,?,?,?)",
                    (workflow["mode"], workflow["broker_account_id"], action["id"], workflow["id"]))
            connection.execute("INSERT INTO execution_workflows VALUES (?,?)", (workflow["id"], _dump(workflow)))
            self._event(connection, workflow["id"], "CREATED", "신호·계좌·예산·주문 순서를 고정했습니다.")
        return self.get(workflow["id"])

    def mutate(self, workflow_id, change, event=None, detail=""):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload FROM execution_workflows WHERE id=?", (workflow_id,)).fetchone()
            if row is None:
                raise AutomationBlocked("실행 기록을 찾을 수 없습니다.")
            workflow = json.loads(row[0])
            result = change(workflow)
            workflow["updated_at"] = datetime.now(timezone.utc).isoformat()
            connection.execute("UPDATE execution_workflows SET payload=? WHERE id=?", (_dump(workflow), workflow_id))
            if event:
                self._event(connection, workflow_id, event, detail)
        return result

    def set(self, workflow_id, **changes):
        event = changes.pop("event", None)
        detail = changes.get("reason", "")
        self.mutate(workflow_id, lambda workflow: workflow.update(changes), event, detail)

    def order(self, workflow_id, leg_id, **changes):
        def change(workflow):
            order = next(item for item in workflow["orders"] if item["leg_id"] == leg_id)
            order.update(_wire(changes))
        self.mutate(workflow_id, change, changes.get("status"), leg_id)

    def claim_intent(self, workflow_id, leg_id, request):
        def change(workflow):
            if workflow["status"] not in {"RUNNING", "WAIT_CASH"}:
                return False
            order = next(item for item in workflow["orders"] if item["leg_id"] == leg_id)
            if order["status"] != "PLANNED":
                return False
            order.update(status="INTENT", request=_wire(request), quantity=request.quantity)
            return True
        return self.mutate(workflow_id, change, "INTENT", leg_id)


class AutomationEngine:
    def __init__(self, journal: ExecutionJournal, broker: BrokerExecutor, *, now=None):
        self.journal = journal
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._stopped = threading.Event()
        self._armed = None
        self.broker = broker
        for workflow in self.journal.list(10000):
            if workflow["mode"] != "LIVE" or workflow["status"] in TERMINAL:
                continue
            def recover(item):
                item.update(status="REVIEW_REQUIRED", reason="앱 재시작: NH 주문내역을 읽기 전용으로 대조하고 직접 재개하세요.")
                for order in item["orders"]:
                    if order["status"] == "INTENT":
                        order.update(status="UNKNOWN", error="제출 직전 또는 직후 중단되었습니다. 재전송 금지.")
            self.journal.mutate(workflow["id"], recover, "RESTART", "자동 재개 차단")

    @property
    def broker(self):
        return self._broker

    @broker.setter
    def broker(self, value):
        self._broker = value
        bind = getattr(value, "set_stop_event", None)
        if callable(bind):
            bind(self._stopped)

    def arm(self, policy: ExecutionPolicy):
        self._validate_policy(policy)
        if getattr(self.broker, "account_id", None) != policy.broker_account_id:
            raise AutomationBlocked("연결한 NH 계좌와 실행 계좌가 다릅니다.")
        self._armed = (policy.account_id, policy.broker_account_id, _number(policy.max_order_usd, positive=True),
                       _number(policy.max_buy_usd, positive=True))
        self._stopped.clear()

    def activate_dry_run(self):
        self._armed = None
        self._stopped.clear()

    def stop(self):
        """Prevent every subsequent write; already accepted orders remain at NH."""
        self._stopped.set()
        self._armed = None

    def _assert_armed(self, policy):
        armed = self._armed
        if self._stopped.is_set() or not armed or armed[:2] != (policy.account_id, policy.broker_account_id):
            raise AutomationBlocked("실주문은 이 앱 실행 중 선택 계좌에 직접 허용한 경우에만 가능합니다.")
        if _number(policy.max_order_usd) > armed[2] or _number(policy.max_buy_usd) > armed[3]:
            raise AutomationBlocked("허용한 실주문 예산을 늘렸습니다. 중지 후 금액을 확인하고 다시 허용하세요.")

    def _validate_policy(self, policy):
        if policy.mode not in {"LIVE", "DRY_RUN"} or not policy.account_id or not policy.broker_account_id:
            raise AutomationBlocked("실행 방식과 계좌 하나를 확인하세요.")
        _number(policy.max_order_usd, positive=True)
        _number(policy.max_buy_usd, positive=True)
        if not ZERO <= _number(policy.fee_reserve_pct) <= Decimal("0.1"):
            raise AutomationBlocked("수수료 여유율은 0~10%입니다.")
        if not Decimal("1") <= _number(policy.buy_multiplier) <= Decimal("1.01") or not Decimal("0.99") <= _number(policy.sell_multiplier) <= Decimal("1"):
            raise AutomationBlocked("지정가 조정 배율을 확인하세요.")
        for value in (policy.quote_max_age_seconds, policy.snapshot_max_age_seconds, policy.evaluation_max_age_seconds):
            if type(value) is not int or not 1 <= value <= 600:
                raise AutomationBlocked("자료 유효 시간은 1~600초입니다.")
        if not 1 <= policy.signal_max_age_hours <= 120 or not 0 <= policy.close_buffer_seconds <= 600:
            raise AutomationBlocked("신호 유효기간 또는 장 마감 여유시간을 확인하세요.")
        if policy.deadline_at is not None:
            _utc(policy.deadline_at)

    def _fresh(self, value, maximum, label):
        age = (_utc(self.now()) - _utc(value)).total_seconds()
        if age < -5 or age > maximum:
            raise AutomationBlocked(label + " 자료가 오래되었거나 미래 시각입니다.")

    def _validate_signal(self, decision, policy, evidence):
        _number(evidence.exit_reserve_cash_usd)
        if decision.account_id != policy.account_id or evidence.account_id != policy.account_id:
            raise AutomationBlocked("전략·자료·실행 계좌가 서로 다릅니다.")
        if not evidence.source or evidence.history_hash != decision.history_hash or evidence.as_of != decision.as_of:
            raise AutomationBlocked("확인한 종가 자료와 현재 전략 신호가 다릅니다.")
        if decision.blockers or decision.manual_review:
            raise AutomationBlocked("전략에 미확인 자료·수량·복수 임계값이 있습니다. 전략 화면의 확인 항목을 처리하세요.")
        if _number(decision.quantity_step, positive=True) != 1:
            raise AutomationBlocked("자동 주문은 정수 주수만 지원합니다.")
        self._fresh(decision.evaluated_at, policy.evaluation_max_age_seconds, "전략 평가")
        signal_day = date.fromisoformat(decision.as_of)
        age = (_utc(self.now()).date() - signal_day).days * 24
        if age < 0 or age > policy.signal_max_age_hours:
            raise AutomationBlocked("완료 종가 신호의 유효기간이 지났습니다.")
        if policy.mode == "LIVE":
            self._assert_armed(policy)
            self._fresh(evidence.verified_at, policy.evaluation_max_age_seconds, "신호 확인")
            if not (evidence.completed and evidence.adjustment_confirmed and evidence.latest_session_verified):
                raise AutomationBlocked("최신 완료 정규장과 분할 보정 여부를 먼저 확인하세요.")
            if evidence.expected_session_date != evidence.as_of:
                raise AutomationBlocked("거래소 달력의 최신 완료 거래일과 신호 날짜가 다릅니다.")
            if any(action.kind == "SELL_PROFIT" for action in decision.actions) and not evidence.broker_return_verified:
                raise AutomationBlocked("환차손을 포함한 동일 날짜의 증권사 수익률이 확인되지 않았습니다.")

    def start(self, decision, policy: ExecutionPolicy, evidence: SignalEvidence):
        with self._lock:
            self._validate_policy(policy)
            self._validate_signal(decision, policy, evidence)
            if self._stopped.is_set():
                raise AutomationBlocked("자동 실행을 다시 시작하세요.")
            if getattr(self.broker, "account_id", None) != policy.broker_account_id:
                raise AutomationBlocked("연결한 NH 계좌와 실행 계좌가 다릅니다.")
            key = [policy.mode, policy.account_id, policy.broker_account_id, decision.history_hash,
                   [action.id for action in decision.actions]]
            if policy.mode == "DRY_RUN":
                key.append(_wire(policy))
            identity = hashlib.sha256(_dump(key).encode()).hexdigest()[:32]
            orders = self._plan(decision, evidence)
            now = _utc(self.now()).isoformat()
            workflow = {"id": identity, "owner": self.journal.machine_id, "account_id": policy.account_id,
                "broker_account_id": policy.broker_account_id, "mode": policy.mode,
                "status": "RUNNING" if orders else "NO_ACTION", "reason": "",
                "created_at": now, "updated_at": now, "applied": False,
                "decision": _wire(decision), "policy": _wire(policy), "evidence": _wire(evidence),
                "orders": orders, "funding_baselines": {},
                "baseline_holdings": {symbol: str(_number(decision.positions.get(symbol, {}).get("quantity", "0"))) for symbol in SYMBOLS},
                "deadline_at": _wire(policy.deadline_at)}
            return self.journal.begin(workflow)

    def _plan(self, decision, evidence):
        orders = []
        def add(action, suffix, symbol, side, *, quantity=None, budget=None, funding=(), kind=None):
            leg_id = action.id + ":" + suffix
            order = {"leg_id": leg_id, "action_id": action.id, "kind": kind or action.kind,
                "symbol": symbol, "side": side, "quantity": int(quantity) if quantity is not None else None,
                "budget_usd": str(budget) if budget is not None else None, "funding": list(funding),
                "status": "PLANNED", "broker_order_id": None, "request": None, "update": None}
            orders.append(order)
            return leg_id
        exits = []
        for action in decision.actions:
            if action.symbol not in SYMBOLS or action.kind not in {"BUY_ENTRY", "SELL_PROFIT", "EXIT", "BUY_SPYM"}:
                raise AutomationBlocked("지원하지 않는 자동 주문 단계입니다.")
            if action.quantity is not None:
                quantity = _number(action.quantity, positive=True)
                if quantity != quantity.to_integral_value():
                    raise AutomationBlocked("자동 주문 수량은 1주 단위여야 합니다.")
            if action.kind == "BUY_ENTRY":
                if action.symbol != "TQQQ":
                    raise AutomationBlocked("분할 진입 종목이 TQQQ가 아닙니다.")
                budget = _number(action.budget_usd, positive=True)
                funding = ()
                if evidence.entry_funding_from_sgov:
                    funding = (add(action, "fund-sgov", "SGOV", "SELL", budget=budget, kind="FUND_SGOV"),)
                add(action, "buy", "TQQQ", "BUY", budget=budget, funding=funding)
            elif action.kind == "BUY_SPYM":
                if action.symbol != "SPYM":
                    raise AutomationBlocked("익절 대기금의 대상이 SPYM이 아닙니다.")
                add(action, "buy", "SPYM", "BUY", budget=_number(action.budget_usd, positive=True))
            else:
                leg = add(action, "sell", action.symbol, "SELL", quantity=action.quantity)
                if action.kind == "EXIT":
                    exits.append(leg)
                else:
                    if action.symbol != "TQQQ":
                        raise AutomationBlocked("익절 매도 종목이 TQQQ가 아닙니다.")
                    add(action, "rotate-spym", "SPYM", "BUY", funding=(leg,), kind="ROTATE_SPYM")
        if exits:
            if any(action.kind not in {"EXIT"} for action in decision.actions):
                raise AutomationBlocked("하락 전환과 다른 전략 주문을 같은 실행에 혼합할 수 없습니다.")
            target = add(decision.actions[0], "rotate-sgov", "SGOV", "BUY", funding=exits, kind="ROTATE_SGOV")
            next(order for order in orders if order["leg_id"] == target)["reserve_cash_usd"] = str(evidence.exit_reserve_cash_usd)
        for target in orders:
            for source_id in target["funding"]:
                next(item for item in orders if item["leg_id"] == source_id)["funding_target"] = target["leg_id"]
        return orders

    def _policy(self, workflow):
        data = dict(workflow["policy"])
        for key in ("max_order_usd", "max_buy_usd", "fee_reserve_pct", "buy_multiplier", "sell_multiplier"):
            data[key] = Decimal(data[key])
        if data.get("deadline_at"):
            data["deadline_at"] = _utc(data["deadline_at"])
        return ExecutionPolicy(**data)

    def _expected_holdings(self, workflow):
        expected = {symbol: _number(value) for symbol, value in workflow["baseline_holdings"].items()}
        for order in workflow["orders"]:
            update = order.get("update") or {}
            qty = _number(update.get("filled_quantity", "0"))
            expected[order["symbol"]] += qty if order["side"] == "BUY" else -qty
        return expected

    def _snapshot(self, workflow, policy):
        snapshot = self.broker.snapshot(policy.broker_account_id, SYMBOLS)
        if snapshot.account_id != policy.broker_account_id:
            raise AutomationBlocked("증권사 조회 계좌가 실행 계좌와 다릅니다.")
        self._fresh(snapshot.captured_at, policy.snapshot_max_age_seconds, "잔고·현금·미체결")
        _number(snapshot.available_usd)
        if policy.mode == "LIVE":
            if not snapshot.data_verified:
                raise AutomationBlocked("증권사 잔고·주문 가능 USD·미체결 조회의 완전성을 확인할 수 없습니다.")
            if snapshot.pending_orders:
                raise AutomationBlocked("증권사에 미체결 주문이 있습니다. 기존 주문과 이 실행을 대조하세요.")
            if not snapshot.market_open or not snapshot.market_close_at:
                raise AutomationBlocked("현재 거래 세션과 마감 시각을 확인할 수 없습니다.")
            expected = self._expected_holdings(workflow)
            if any(_number(snapshot.holdings.get(symbol, "0")) != expected[symbol] for symbol in SYMBOLS):
                raise AutomationBlocked("TQQQ·SPYM·SGOV 실제 잔고와 원장/확인 체결이 다릅니다. 대조 후 재개하세요.")
            close = _utc(snapshot.market_close_at) - timedelta(seconds=policy.close_buffer_seconds)
            deadline = min(close, _utc(workflow["deadline_at"])) if workflow.get("deadline_at") else close
            if _utc(self.now()) >= deadline:
                raise AutomationBlocked("주문 마감 시각을 지났습니다.")
            self.journal.set(workflow["id"], deadline_at=deadline.isoformat())
        elif "simulation_cash" in workflow:
            snapshot = BrokerSnapshot(snapshot.account_id, snapshot.captured_at, self._expected_holdings(workflow),
                Decimal(workflow["simulation_cash"]), (), snapshot.market_open, snapshot.market_close_at, snapshot.data_verified)
        return snapshot

    def _quote(self, policy, symbol):
        quote = self.broker.quote(policy.broker_account_id, symbol)
        if quote.symbol != symbol or quote.currency != "USD":
            raise AutomationBlocked("주문 종목·통화와 시세가 다릅니다.")
        self._fresh(quote.captured_at, policy.quote_max_age_seconds, "현재가")
        _number(quote.price, positive=True)
        return quote

    def _remaining_buy(self, workflow, policy):
        spent = ZERO
        for order in workflow["orders"]:
            if order["side"] == "BUY" and order.get("update"):
                update = order["update"]
                if update.get("average_price"):
                    spent += _number(update["filled_quantity"]) * _number(update["average_price"]) * (1 + policy.fee_reserve_pct)
        return max(ZERO, policy.max_buy_usd - spent)

    def _net_proceeds(self, order, policy):
        update = order.get("update") or {}
        amount = _number(update.get("filled_quantity", "0")) * _number(update.get("average_price", "0"))
        if update.get("commission_usd") is not None and update.get("tax_usd") is not None:
            return max(ZERO, amount - _number(update["commission_usd"]) - _number(update["tax_usd"]))
        return amount * (1 - policy.fee_reserve_pct)

    def _request(self, workflow, order, policy, snapshot):
        quote = self._quote(policy, order["symbol"])
        multiplier = policy.buy_multiplier if order["side"] == "BUY" else policy.sell_multiplier
        price = (quote.price * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if price <= 0:
            raise AutomationBlocked("지정가가 최소 가격 단위보다 작습니다.")
        available = _number(snapshot.available_usd)
        buy_cap = min(policy.max_order_usd, self._remaining_buy(workflow, policy))
        if order.get("funding_target") and order["funding_target"] not in workflow["funding_baselines"]:
            target = next(item for item in workflow["orders"] if item["leg_id"] == order["funding_target"])
            if _number(target.get("reserve_cash_usd", "0")) > available:
                raise AutomationBlocked("하락 전환에 배정한 기존 현금이 실제 매도 전 USD 가용 현금보다 큽니다.")
        if order["kind"] in {"BUY_ENTRY", "FUND_SGOV"} and _number(order["budget_usd"], positive=True) > buy_cap:
            raise AutomationBlocked("이번 분할 매수 예산이 주문/총 매수 상한을 넘습니다. 일부만 집행하고 차수를 완료하지 않습니다.")
        if order["kind"] == "FUND_SGOV":
            budget = min(_number(order["budget_usd"], positive=True), buy_cap)
            if available >= budget:
                self.journal.order(workflow["id"], order["leg_id"], status="SKIPPED", quantity=0)
                return None
            quantity = int(((budget - available) / (price * (1 - policy.fee_reserve_pct))).to_integral_value(rounding=ROUND_CEILING))
        elif order["side"] == "SELL":
            quantity = int(order["quantity"])
        else:
            budget = min(_number(order["budget_usd"]) if order["budget_usd"] else buy_cap, buy_cap)
            funding = [next(item for item in workflow["orders"] if item["leg_id"] == identity) for identity in order["funding"]]
            if any(item["status"] not in SUCCESS for item in funding):
                raise AutomationBlocked("선행 매도의 전량 체결을 확인하지 못했습니다.")
            if funding and order["kind"] in {"ROTATE_SPYM", "ROTATE_SGOV"}:
                proceeds = sum((self._net_proceeds(item, policy) for item in funding), ZERO)
                baseline = _number(workflow["funding_baselines"].get(order["leg_id"], "0"))
                reserve = _number(order.get("reserve_cash_usd", "0")) if order["kind"] == "ROTATE_SGOV" else ZERO
                if reserve > baseline:
                    raise AutomationBlocked("배정 현금이 매도 전 확인한 가용 USD를 넘습니다.")
                budget = min(budget, proceeds + reserve)
                if available - baseline + reserve < budget:
                    self.journal.set(workflow["id"], status="WAIT_CASH", reason="매도는 전량 체결되었습니다. 실제 주문 가능 USD 증가를 기다립니다.")
                    return None
            if available < budget:
                self.journal.set(workflow["id"], status="WAIT_CASH", reason="정해진 매수 예산에 필요한 실제 주문 가능 USD를 기다립니다.")
                return None
            quantity = int((budget / (price * (1 + policy.fee_reserve_pct))).to_integral_value(rounding=ROUND_FLOOR))
        if quantity <= 0:
            raise AutomationBlocked("예산으로 1주를 살 수 없습니다. 자동으로 단계를 완료하지 않습니다.")
        if order["side"] == "SELL" and Decimal(quantity) > _number(snapshot.holdings.get(order["symbol"], "0")):
            raise AutomationBlocked("실제 보유 수량이 매도 계획보다 적습니다.")
        if Decimal(quantity) * price > policy.max_order_usd:
            raise AutomationBlocked("전량 매도 금액이 1회 주문 상한을 넘습니다. 분할 축소 없이 실행을 보류합니다.")
        if order.get("funding_target") and order["funding_target"] not in workflow["funding_baselines"]:
            def baseline(item):
                item["funding_baselines"].setdefault(order["funding_target"], str(available))
            self.journal.mutate(workflow["id"], baseline)
        return OrderRequest(policy.broker_account_id, order["symbol"], order["side"], quantity, price,
                            workflow["id"] + ":" + order["leg_id"])

    def _observe(self, workflow, order, update):
        status = str(update.status).upper()
        if status not in {"SUBMITTED", "FILLED", "PARTIAL", "REJECTED", "UNKNOWN", "CANCELLED"}:
            status = "UNKNOWN"
        quantity = _number(update.filled_quantity)
        requested = Decimal(order["quantity"])
        if update.updated_at is None:
            status = "UNKNOWN"
        else:
            try:
                self._fresh(update.updated_at, 600, "주문 상태")
            except AutomationBlocked:
                status = "UNKNOWN"
        if quantity > requested or quantity < _number((order.get("update") or {}).get("filled_quantity", "0")):
            status = "UNKNOWN"
        if status == "FILLED" and (quantity != requested or not update.average_price or _number(update.average_price) <= 0):
            status = "UNKNOWN"
        if quantity > 0 and update.average_price is not None:
            average = _number(update.average_price, positive=True)
            limit = _number((order.get("request") or {}).get("limit_price", "0"))
            if limit and (order["side"] == "BUY" and average > limit or order["side"] == "SELL" and average < limit):
                status = "UNKNOWN"
        for value in (update.commission_usd, update.tax_usd):
            if value is not None:
                try:
                    _number(value)
                except AutomationBlocked:
                    status = "UNKNOWN"
        if status != "FILLED" and quantity > 0 and status != "UNKNOWN":
            status = "PARTIAL"
        if status in {"SUBMITTED", "FILLED", "PARTIAL"} and not update.broker_order_id:
            status = "UNKNOWN"
        if order.get("broker_order_id") and update.broker_order_id != order["broker_order_id"]:
            status = "UNKNOWN"
        record = _wire(update)
        record["message"] = _safe_message(update.message)
        record["status"] = status
        self.journal.order(workflow["id"], order["leg_id"], status=status, update=record,
            broker_order_id=order.get("broker_order_id") or update.broker_order_id)
        if status in {"UNKNOWN", "PARTIAL", "REJECTED", "CANCELLED"}:
            self.journal.set(workflow["id"], status="BLOCKED", reason={
                "UNKNOWN": "주문 결과가 불명확합니다. 자동 재전송하지 않습니다. NH 주문내역을 대조하세요.",
                "PARTIAL": "일부만 체결되었습니다. 다음 주문은 차단했습니다. NH 주문내역을 대조하세요.",
                "REJECTED": "증권사가 주문을 거절했습니다. 자동 재주문하지 않습니다.",
                "CANCELLED": "주문 취소를 확인했습니다. 다음 주문은 차단했습니다."}[status], event="BLOCKED")

    def tick(self, workflow_id):
        with self._lock:
            workflow = self.journal.get(workflow_id)
            if self._stopped.is_set() or workflow["status"] in TERMINAL:
                return workflow
            policy = self._policy(workflow)
            if policy.mode == "LIVE":
                try:
                    self._assert_armed(policy)
                except AutomationBlocked:
                    return workflow
            if workflow["status"] == "REVIEW_REQUIRED":
                return workflow
            if all(order["status"] in SUCCESS for order in workflow["orders"]):
                self.journal.set(workflow_id, status="COMPLETE" if policy.mode == "LIVE" else "SIMULATED",
                    reason="체결 원장 반영 대기" if policy.mode == "LIVE" else "가상 결과입니다. 실제 원장은 변경하지 않았습니다.", event="FINISHED")
                return self.journal.get(workflow_id)
            if workflow.get("deadline_at") and _utc(self.now()) >= _utc(workflow["deadline_at"]):
                self._deadline(workflow, policy)
                return self.journal.get(workflow_id)
            if workflow["status"] == "BLOCKED":
                return workflow
            order = next(item for item in workflow["orders"] if item["status"] not in SUCCESS)
            try:
                if order["status"] == "SUBMITTED":
                    update = self.broker.order_status(policy.broker_account_id, order["broker_order_id"])
                    self._observe(workflow, order, update)
                    return self.journal.get(workflow_id)
                if order["status"] != "PLANNED":
                    raise AutomationBlocked("제출 이력이 있는 주문은 재전송할 수 없습니다. 상태를 대조하세요.")
                snapshot = self._snapshot(workflow, policy)
                workflow = self.journal.get(workflow_id)
                request = self._request(workflow, order, policy, snapshot)
                if request is None:
                    return self.journal.get(workflow_id)
                if policy.mode == "DRY_RUN":
                    gross = Decimal(request.quantity) * request.limit_price
                    def simulate(item):
                        current = next(leg for leg in item["orders"] if leg["leg_id"] == order["leg_id"])
                        current.update(status="SIMULATED", quantity=request.quantity, request=_wire(request),
                            update=_wire(OrderUpdate("SIMULATED:" + request.intent_id, "SIMULATED", Decimal(request.quantity),
                                request.limit_price, _utc(self.now()), commission_usd=ZERO, tax_usd=ZERO)))
                        item["simulation_cash"] = str(snapshot.available_usd + (gross if request.side == "SELL" else -gross))
                        item.update(status="RUNNING", reason="가상 체결이며 실제 계좌·원장에 반영하지 않습니다.")
                    self.journal.mutate(workflow_id, simulate, "SIMULATED", order["leg_id"])
                    return self.journal.get(workflow_id)
                self._assert_armed(policy)
                if self._stopped.is_set():
                    return self.journal.get(workflow_id)
                if not self.journal.claim_intent(workflow_id, order["leg_id"], request):
                    return self.journal.get(workflow_id)
                # This is the only submission site. A durable intent already exists.
                # Recheck after persistence so closing the UI cannot start a later leg.
                if self._stopped.is_set():
                    self.journal.order(workflow_id, order["leg_id"], status="UNKNOWN", error="제출 직전 중지: 재전송 금지, 상태 대조 필요")
                    raise AutomationBlocked("실행 중지 요청을 받았습니다. 다음 주문을 제출하지 않았습니다.")
                try:
                    update = self.broker.submit_limit(request)
                except Exception as exc:
                    self.journal.order(workflow_id, order["leg_id"], status="UNKNOWN", error=type(exc).__name__)
                    raise AutomationBlocked("주문 응답을 받지 못했습니다. NH에 접수되었을 수 있으므로 자동 재전송하지 않습니다.") from exc
                fresh_order = next(item for item in self.journal.get(workflow_id)["orders"] if item["leg_id"] == order["leg_id"])
                self._observe(workflow, fresh_order, update)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, AutomationBlocked) else "조회 또는 응답 검증 실패: " + type(exc).__name__
                self.journal.set(workflow_id, status="BLOCKED", reason=_safe_message(reason), event="BLOCKED")
            return self.journal.get(workflow_id)

    def _deadline(self, workflow, policy):
        for order in workflow["orders"]:
            if order["status"] not in {"SUBMITTED", "PARTIAL", "UNKNOWN"} or not order.get("broker_order_id") or order.get("cancel_attempted"):
                continue
            if policy.mode == "LIVE" and not self._stopped.is_set():
                self.journal.order(workflow["id"], order["leg_id"], cancel_attempted=True, status="CANCEL_REQUESTED")
                try:
                    update = self.broker.cancel(policy.broker_account_id, order["broker_order_id"])
                    self._observe(workflow, order, update)
                except Exception as exc:
                    self.journal.order(workflow["id"], order["leg_id"], status="UNKNOWN", error="취소 결과 불명: " + type(exc).__name__)
        self.journal.set(workflow["id"], status="BLOCKED", reason="실행 마감: 새 주문을 차단했습니다. 취소 요청은 체결/취소 확정과 다릅니다. NH 주문내역을 대조하세요.", event="DEADLINE")

    def reconcile(self, workflow_id):
        """Read broker truth. Never resubmit, cancel, or automatically resume."""
        with self._lock:
            workflow = self.journal.get(workflow_id)
            if workflow["mode"] != "LIVE":
                return workflow
            if getattr(self.broker, "account_id", None) != workflow["broker_account_id"]:
                raise AutomationBlocked("대조할 NH 계좌가 실행 계좌와 다릅니다.")
            for order in workflow["orders"]:
                if order["status"] in SUCCESS or order["status"] == "PLANNED" or not order.get("broker_order_id"):
                    continue
                try:
                    self._observe(workflow, order, self.broker.order_status(workflow["broker_account_id"], order["broker_order_id"]))
                except Exception as exc:
                    self.journal.order(workflow_id, order["leg_id"], status="UNKNOWN", error="상태 조회 실패: " + type(exc).__name__)
            result = self.journal.get(workflow_id)
            done = all(order["status"] in SUCCESS for order in result["orders"])
            self.journal.set(workflow_id, status="COMPLETE" if done else "REVIEW_REQUIRED",
                reason="실제 체결 원장 반영 대기" if done else "대조 결과를 확인하세요. 자동 재개하지 않습니다.", event="RECONCILED")
            return self.journal.get(workflow_id)

    def resume(self, workflow_id, *, evidence=None):
        """Explicit local continuation after read-only reconciliation, never retry."""
        with self._lock:
            workflow = self.journal.get(workflow_id)
            policy = self._policy(workflow)
            self._assert_armed(policy)
            if evidence is None:
                self._fresh(workflow["evidence"]["verified_at"], policy.evaluation_max_age_seconds, "재개 신호")
            else:
                decision = decision_from_workflow(workflow)
                decision.evaluated_at = _utc(evidence.verified_at).isoformat()
                if evidence.source != workflow["evidence"]["source"]:
                    raise AutomationBlocked("실행 중 가격 자료 출처를 바꿀 수 없습니다.")
                self._validate_signal(decision, policy, evidence)
            if any(order["status"] not in SUCCESS | {"PLANNED"} for order in workflow["orders"]):
                raise AutomationBlocked("미확정·부분 체결·거절·취소 주문이 있습니다. 자동 재개할 수 없습니다.")
            self._snapshot(workflow, policy)
            self.journal.set(workflow_id, status="RUNNING", reason="대조 후 이 PC에서 재개를 직접 요청했습니다.", event="RESUMED")
            return self.journal.get(workflow_id)

    def mark_applied(self, workflow_id):
        workflow = self.journal.get(workflow_id)
        if workflow["mode"] != "LIVE" or workflow["status"] != "COMPLETE":
            raise AutomationBlocked("확정된 실체결 실행만 실제 원장 반영 완료로 표시할 수 있습니다.")
        self.journal.set(workflow_id, applied=True, reason="실제 NH 체결을 중복 제거하여 원장·전략 단계에 반영했습니다.", event="APPLIED")
