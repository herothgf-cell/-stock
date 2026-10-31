"""Join durable execution workflows to actual NH records and journal state.

No proposed order becomes a trade. A complete workflow must be matched to the
broker's cumulative execution rows before its strategy stage advances.
"""
import copy
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
import hashlib
from zoneinfo import ZoneInfo

from .broker_accounts import broker_import_preview, is_nh_account
from .domain import decimal_value, validate_state
from .strategy import Action, FIELD, confirm_action, evaluate
from .workflow import stage_import


SYMBOLS = ('TQQQ', 'SPYM', 'SGOV')
ZERO = Decimal(0)


def _decoder(decode_handle=None):
    if decode_handle is not None:
        return decode_handle
    from .nh_execution import decode_order_handle
    return decode_order_handle


def _orders(workflow, decode_handle=None):
    if workflow.get('mode') != 'LIVE' or workflow.get('status') != 'COMPLETE':
        raise ValueError('실제 전량 체결을 확인한 실행만 원장에 반영할 수 있습니다.')
    decode = _decoder(decode_handle)
    result = []
    for order in workflow.get('orders', []):
        if order.get('status') == 'SKIPPED' and order.get('kind') == 'FUND_SGOV':
            continue
        if order.get('status') != 'FILLED' or not order.get('broker_order_id'):
            raise ValueError('부분 체결·불명 주문은 자동으로 완료 처리하지 않습니다.')
        handle = decode(order['broker_order_id'])
        if handle.get('account_id') != workflow.get('broker_account_id') or handle.get('environment') != 'live':
            raise ValueError('실행 기록과 실제 NH 계좌가 다릅니다.')
        if (handle.get('symbol'), handle.get('side')) != (order.get('symbol'), order.get('side')):
            raise ValueError('주문 식별자의 종목·매매 방향이 실행 기록과 다릅니다.')
        result.append((order, handle))
    if not result:
        raise ValueError('확인할 실제 체결이 없습니다.')
    return result


def execution_dates(workflow, *, decode_handle=None):
    dates = {day for _, handle in _orders(workflow, decode_handle) for day in handle['candidate_dates']}
    for day in dates:
        date.fromisoformat(day)
    return sorted(dates)


def apply_verified_fills(state, workflow, rows, *, decode_handle=None, now=None):
    """Atomically import exact actual fills, confirm stages and mark application."""
    with localcontext() as context:
        context.prec = 50
        return _apply(state, workflow, rows, decode_handle, now or datetime.now(timezone.utc))


def _apply(state, workflow, rows, decode_handle, now):
    from .automation import decision_from_workflow
    validate_state(state)
    pairs = _orders(workflow, decode_handle)
    account_id = workflow['account_id']
    account = state['accounts'].get(account_id)
    if not account or account.get('deleted') or not is_nh_account(account):
        raise ValueError('연결된 나무 원장 계좌를 확인하세요.')
    config = account.get(FIELD, {})
    if workflow['id'] in config.get('automation_applied', []):
        return copy.deepcopy(state)
    decision = decision_from_workflow(workflow)
    if decision.account_id != account_id:
        raise ValueError('실행과 전략 계획의 계좌가 다릅니다.')
    selected, by_leg, execution_evidence, unknown_costs = [], {}, {}, False
    for order, handle in pairs:
        candidate_ids = {f"nh-us:{handle['account_id'][:24]}:{day}:{handle['order_number']}" for day in handle['candidate_dates']}
        matches = [row for row in rows if row.get('id') in candidate_ids]
        if len(matches) != 1:
            raise ValueError('NH 체결과 주문 식별자를 유일하게 대조하지 못했습니다. 주문일자·체결 내역을 확인하세요.')
        row = copy.deepcopy(matches[0]); update = order.get('update') or {}
        quantity = decimal_value(row.get('quantity'), '실제 체결 수량', positive=True)
        price = decimal_value(row.get('price'), '실제 체결 단가', positive=True)
        if row.get('source') != 'NH' or row.get('currency') != 'USD' or row.get('instrument_id') != 'US:' + handle['symbol'] or row.get('side') != handle['side']:
            raise ValueError('NH 체결의 종목·통화·매매 방향이 주문과 다릅니다.')
        quantities = (handle['quantity'], order.get('quantity'), update.get('filled_quantity'))
        if any(quantity != decimal_value(str(value), '주문 체결 수량', positive=True) for value in quantities):
            raise ValueError('주문 전량과 실제 누적 체결 수량이 다릅니다.')
        limit = decimal_value(handle['limit_price'], '주문 지정가', positive=True)
        if (handle['side'] == 'BUY' and price > limit) or (handle['side'] == 'SELL' and price < limit):
            raise ValueError('실제 평균 체결가가 주문 지정가 범위를 벗어났습니다.')
        if row['id'] in by_leg.values():
            raise ValueError('하나의 실제 체결을 여러 주문 단계에 재사용할 수 없습니다.')
        known_costs = update.get('commission_usd') is not None and update.get('tax_usd') is not None
        if known_costs:
            row['fee'] = format(decimal_value(update['commission_usd'], '체결 수수료'), 'f')
            row['tax'] = format(decimal_value(update['tax_usd'], '체결 세금'), 'f')
            row['cost_verification'] = 'NH order status'
        else:
            unknown_costs = True
            row['cost_verification'] = 'review_required'
        row['import_notes'] = list(row.get('_warnings', []))
        if not known_costs:
            row['import_notes'].append('자동 실행 체결: 비용은 미확정입니다. 실제 명세서와 남은 전략 예산을 확인하세요.')
        previous = state['trades'].get(row['id'])
        if previous:
            if previous.get('deleted') or any(str(previous.get(key)) != str(row.get(key)) for key in ('quantity','price','instrument_id','side','executed_at')):
                raise ValueError('같은 주문의 기존 기록과 실제 체결이 다릅니다. NH 조회에서 누적 갱신을 먼저 검토하세요.')
            if known_costs and any(Decimal(previous[key]) != Decimal(row[key]) for key in ('fee','tax')):
                raise ValueError('기존에 수정한 비용과 NH 확인 비용이 다릅니다. 비용을 먼저 대조하세요.')
        by_leg[order['leg_id']] = row['id']; selected.append(row)
        execution_evidence[row['id']] = {'submitted_at':handle['submitted_at'], 'observed_at':update.get('updated_at')}
    preview = broker_import_preview(state, account_id, selected)
    if any(row.get('_blocked') for row in preview.trades):
        raise ValueError('기존 수동 기록·기초 보유와 중복 가능성이 있어 자동 반영을 보류했습니다. NH 체결 가져오기에서 검토하세요.')
    result, _ = stage_import(state, preview)
    for action in decision.actions:
        ids = [by_leg[order['leg_id']] for order, _ in pairs
               if order['action_id'] == action.id and order['kind'] == action.kind]
        if not ids:
            raise ValueError('원래 전략 단계에 대응하는 실제 체결을 찾을 수 없습니다.')
        result = confirm_action(result, account_id, decision, action.id, ids, execution_evidence=execution_evidence, now=now)
    for order, _ in pairs:
        if order['kind'] != 'ROTATE_SPYM':
            continue
        cycle = result['accounts'][account_id][FIELD].get('cycle')
        if not cycle:
            raise ValueError('SPYM 회전에 연결할 익절 사이클이 없습니다.')
        action = Action('automation:' + workflow['id'] + ':' + order['leg_id'], 'BUY_SPYM', 'SPYM', None,
                        Decimal(cycle['pending_spym_usd']), '확인한 익절 체결의 SPYM 재투자')
        rotation = replace(decision, cycle=copy.deepcopy(cycle), actions=[action])
        result = confirm_action(result, account_id, rotation, action.id, [by_leg[order['leg_id']]], execution_evidence=execution_evidence, now=now)
    target = result['accounts'][account_id][FIELD]
    if any(action.kind == 'EXIT' for action in decision.actions):
        if unknown_costs:
            target['reserve_review_required'] = True
        else:
            proceeds = sum((Decimal(result['trades'][by_leg[order['leg_id']]]['quantity']) * Decimal(result['trades'][by_leg[order['leg_id']]]['price'])
                           - Decimal(result['trades'][by_leg[order['leg_id']]]['fee']) - Decimal(result['trades'][by_leg[order['leg_id']]]['tax'])
                           for order, _ in pairs if order['kind'] == 'EXIT'), ZERO)
            parking_costs = sum((Decimal(result['trades'][by_leg[order['leg_id']]]['fee']) + Decimal(result['trades'][by_leg[order['leg_id']]]['tax'])
                                 for order, _ in pairs if order['kind'] == 'ROTATE_SGOV'), ZERO)
            target['cash_usd'] = format(max(ZERO, Decimal(config.get('cash_usd', '0')) + proceeds - parking_costs), 'f')
    if unknown_costs:
        target['costs_review_required'] = True
        target['costs_review_note'] = 'NH 체결 비용이 미확정입니다. 실제 비용·남은 분할 매수 예산·SPYM 대기금을 확인한 뒤 다음 자동 단계를 허용하세요.'
    target.setdefault('automation_applied', []).append(workflow['id'])
    result['accounts'][account_id]['updated_at'] = now.isoformat()
    validate_state(result)
    return result


def prepare_context(state, account_id, number, provider, history, *, mode='DRY_RUN', cash_confirmed=False, now=None):
    """Read-only context construction. Its returned adapter writes only when armed."""
    with localcontext() as context:
        context.prec = 50
        return _prepare_context(state, account_id, number, provider, history, mode, cash_confirmed, now)


def _prepare_context(state, account_id, number, provider, history, mode, cash_confirmed, fixed_now):
    from .automation import AutomationContext, SignalEvidence
    from .market_calendar import latest_completed, session as market_session
    from .nh_execution import NHExecutionAdapter
    now = fixed_now or datetime.now(timezone.utc)
    account = state['accounts'].get(account_id)
    if not account or account.get('deleted') or not is_nh_account(account) or account.get('kind') != '일반':
        raise ValueError('자동 실행은 연결된 나무 일반 미국 현금 거래 계좌에서 지원합니다.')
    expected_binding = hashlib.sha256(('nh-account:' + number).encode()).hexdigest()
    if account.get('broker_fingerprint') != expected_binding:
        raise ValueError('이 PC의 실제 계좌 연결을 다시 확인하세요.')
    matches = [row for row in provider.discover_accounts() if row['account_number'] == number and row['usable'] and row['environment'] == 'live']
    if len(matches) != 1:
        raise ValueError('NH 공식 계좌 목록에서 연결 계좌를 유일하게 확인하지 못했습니다.')
    local = now.astimezone(ZoneInfo('America/New_York'))
    session = market_session(local.date())
    active = bool(session and session['open'] <= local < session['after_close'])
    completed = latest_completed(now)
    after_close = bool(session and session['close'] <= local < session['after_close'] and completed == local.date().isoformat())
    if mode == 'LIVE' and not after_close:
        raise ValueError('자동 실행은 같은 거래일 정규장 종료 10분 뒤부터 시간외 종료 전까지입니다. 휴장일·장중에는 완료 신호를 기다립니다.')
    period = 'afterhours' if session and session['close'] <= local < session['after_close'] else 'regular'
    broker = NHExecutionAdapter(provider, matches[0], environment='live', enabled=mode == 'LIVE',
        session=period, session_verified=active, cash_account_verified=cash_confirmed)
    if not history:
        raise ValueError('200티큐 판단 화면에서 종가 자료를 받고 검토한 뒤 시작하세요.')
    current = provider.overseas_daily_prices('TQQQ') if str(history.get('source', '')).startswith('NH') else copy.deepcopy(history)
    snapshot = broker.snapshot(broker.account_id, SYMBOLS)
    if (mode == 'LIVE' and not snapshot.data_verified) or snapshot.account_id != broker.account_id:
        raise ValueError('NH 현금 보유·주문 가능 금액·현재 거래 상태를 확인하지 못했습니다.')
    available = snapshot.available_usd
    sgov = snapshot.holdings.get('SGOV', ZERO)
    sgov_value = ZERO
    if sgov:
        quote = broker.quote(broker.account_id, 'SGOV')
        now = fixed_now or datetime.now(timezone.utc)
        if quote.symbol != 'SGOV' or not -5 <= (now - quote.captured_at).total_seconds() <= 60:
            raise ValueError('SGOV 자금 계산에 사용할 최신 시세를 확인하지 못했습니다.')
        sgov_value = sgov * quote.price
        available += sgov_value
    now = fixed_now or datetime.now(timezone.utc)
    config = account.get(FIELD, {})
    configured_reserve = decimal_value(config.get('cash_usd', '0'), '확인한 전략 예산')
    reserve = min(configured_reserve, available)
    decision = evaluate(state, account_id, current, cash_usd=reserve, now=now)
    expected = latest_completed(now)
    evidence = SignalEvidence(account_id, decision.history_hash, decision.as_of, current.get('source',''), expected,
        bool(current.get('finalization') in ('completed_sessions','historical_dates_only','conservative_regular_close','user_verified')),
        config.get('history_confirmation', {}).get('hash') == decision.history_hash,
        decision.as_of == expected, now, broker_return_verified=decision.broker_return_pct is not None,
        entry_funding_from_sgov=sgov > 0,
        exit_reserve_cash_usd=min(snapshot.available_usd, max(ZERO, configured_reserve - sgov_value)))
    return AutomationContext(decision, evidence, broker)


class AutomationBridge:
    def __init__(self, app):
        self.app = app
        self.pending_fills = set()

    def prepare(self):
        app = self.app
        account_id = app.account_id()
        if not account_id:
            raise ValueError('상단에서 자동 실행할 계좌 하나를 선택하세요.')
        state, history = copy.deepcopy(app.state), copy.deepcopy(app.strategy_page.history)
        number, provider = app.broker.number(account_id), app.provider()
        mode, cash = app.automation_panel.mode.get(), app.automation_panel.cash_account_verified.get()
        return lambda: self._read(lambda: prepare_context(state, account_id, number, provider, history, mode=mode, cash_confirmed=cash))

    @staticmethod
    def _read(function):
        from .automation import AutomationBlocked
        from .providers import ProviderError
        try:
            return function()
        except (ValueError, ProviderError) as exc:
            raise AutomationBlocked(str(exc)) from None

    def prepare_broker(self):
        """Recovery reads must work after market close without a new signal."""
        app = self.app
        account_id = app.account_id()
        if not account_id:
            raise ValueError('대조할 원래 계좌를 선택하세요.')
        account = copy.deepcopy(app.state['accounts'][account_id])
        number, provider = app.broker.number(account_id), app.provider()
        def make():
            from .nh_execution import NHExecutionAdapter
            binding = hashlib.sha256(('nh-account:' + number).encode()).hexdigest()
            if account.get('broker_fingerprint') != binding:
                raise ValueError('대조할 실제 계좌 연결을 다시 확인하세요.')
            matches = [row for row in provider.discover_accounts() if row['account_number'] == number and row['usable'] and row['environment'] == 'live']
            if len(matches) != 1:
                raise ValueError('NH 계좌 목록에서 대조할 계좌를 확인하지 못했습니다.')
            return NHExecutionAdapter(provider, matches[0], environment='live')
        return lambda: self._read(make)

    def fills(self, workflow):
        app = self.app
        uid = workflow['id']
        if uid in self.pending_fills:
            return False
        account_id = workflow['account_id']
        if uid in app.state['accounts'][account_id].get(FIELD, {}).get('automation_applied', []):
            return True
        number, provider = app.broker.number(account_id), app.provider()
        if hashlib.sha256(number.encode('ascii')).hexdigest() != workflow['broker_account_id']:
            raise ValueError('체결을 반영할 실제 계좌 연결이 실행 계좌와 다릅니다.')
        expected = copy.deepcopy(app.state)
        dates = execution_dates(workflow)
        self.pending_fills.add(uid)
        def read():
            rows = []
            for day in dates:
                rows.extend(provider.overseas_trades(number, day, day))
            return apply_verified_fills(expected, workflow, rows)
        def done(state):
            try:
                app.save(state, expected=expected, automation_apply=True)
                app.automation_panel.engine.mark_applied(uid)
                app.automation_panel.refresh()
                app.status.set('NH 실제 체결을 원장과 200티큐 단계에 반영했습니다. 비용 미확정 표시는 전략 화면에서 확인하세요.')
            finally:
                self.pending_fills.discard(uid)
        def failed(error):
            self.pending_fills.discard(uid)
            raise error
        try:
            app.work('자동 주문의 실제 NH 체결 내역을 원장과 대조합니다…', read, done, failed)
        except Exception:
            self.pending_fills.discard(uid)
            raise
        return False
