"""Atomic import staging and stale-edit protection, independent of the GUI."""
import copy
from datetime import datetime, timezone
from uuid import uuid4

COLLECTIONS = ('accounts', 'instruments', 'trades', 'dividends', 'dividend_events')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def record(**fields):
    return {'id': str(uuid4()), 'updated_at': now(), 'deleted': False, **fields}


def clean(row):
    return {k: copy.deepcopy(v) for k, v in row.items() if not k.startswith('_')}


def unchanged(state, collection, opened):
    if opened and (state[collection].get(opened['id']) != opened or opened.get('deleted')):
        raise ValueError('창을 연 뒤 이 기록이 변경되거나 삭제되었습니다. 창을 닫고 최신 기록을 다시 열어 주세요.')


def account_choices(state):
    from collections import Counter
    rows = [r for r in state['accounts'].values() if not r.get('deleted')]
    count = Counter(r['name'] for r in rows)
    return {(r['name'] if count[r['name']] == 1 else f"{r['name']} ({r.get('broker', '')} · {r['id']})"): r['id'] for r in rows}


def groups(preview):
    result = {}
    for row in preview.trades:
        if row.get('source') == 'xlsx:매매일지' and row.get('lot_id'):
            result.setdefault(row['lot_id'], []).append(row)
    return [rows for rows in result.values() if {r['side'] for r in rows} >= {'BUY', 'SELL'}]


def validate_ids(preview):
    for key in COLLECTIONS:
        seen = set()
        for row in getattr(preview, key):
            if not row.get('id') or row['id'] in seen:
                raise ValueError(f'{key}: 원본 식별자가 중복되거나 빠졌습니다. 체결번호와 중복 행을 확인하세요.')
            seen.add(row['id'])


def selectable(preview):
    ids = {r['id'] for r in preview.trades if not r.get('_blocked')}
    for rows in groups(preview):
        if any(r.get('_blocked') for r in rows):
            ids.difference_update(r['id'] for r in rows)
    return ids


def resolve_accounts(current, rows, account_override=None):
    """Resolve aliases once for staging and repeatable initialization imports."""
    accounts = copy.deepcopy(current); account_map = {}
    for row in rows:
        item = record(**clean(row)); uid = item['id']
        if account_override:
            account_map[uid] = account_override; continue
        exact = accounts.get(uid)
        if exact and exact.get('deleted'):
            raise ValueError('삭제된 계좌와 연결됩니다. 저장할 계좌를 지정하세요.')
        named = [r for r in accounts.values() if not r.get('deleted') and r['name'].strip() == item['name'].strip()]
        matches = [r for r in named if all(not item.get(k) or not r.get(k) or item[k] == r[k] for k in ('broker', 'kind', 'currency'))]
        if not exact and named and len(matches) != 1:
            raise ValueError('같은 이름의 계좌가 모호합니다. 저장할 계좌를 직접 지정하세요.')
        target = uid if exact else matches[0]['id'] if matches else uid
        accounts.setdefault(target, item); account_map[uid] = target
    return accounts, account_map


def stage_import(current, preview, selected=None, account_override=None, include_events=True, update_existing=False):
    from .domain import validate_state
    validate_ids(preview)
    chosen = selected if selected is not None else {r['id'] for r in preview.trades if not r.get('_blocked')}
    for rows in groups(preview):
        included = [r for r in rows if r['id'] in chosen and not r.get('_blocked')]
        if included and len(included) != len(rows):
            raise ValueError('매도 완료 물량은 연결된 매수·매도를 함께 검토하고 선택하세요. 한쪽만 저장하지 않았습니다.')
    state = copy.deepcopy(current); stats = {'added': 0, 'updated': 0, 'skipped': 0, 'events': 0}
    state['accounts'], account_map = resolve_accounts(state['accounts'], preview.accounts, account_override)
    for row in preview.instruments:
        item = record(**clean(row)); existing = state['instruments'].get(item['id'])
        if existing and existing.get('deleted'):
            raise ValueError('가져올 기록이 삭제된 종목을 참조합니다. 종목을 확인하세요.')
        if existing and existing['currency'] != item['currency']:
            raise ValueError(f"{item['id']}: 원본과 기존 종목의 통화가 다릅니다.")
        state['instruments'].setdefault(item['id'], item)
    for key in ('trades', 'dividends', 'dividend_events'):
        if key == 'dividend_events' and not include_events:
            continue
        for row in getattr(preview, key):
            if row.get('_blocked') or (key == 'trades' and row['id'] not in chosen):
                stats['skipped'] += 1; continue
            item = record(**clean(row))
            if 'account_id' in item:
                item['account_id'] = account_override or account_map.get(item['account_id'], item['account_id'])
            if row.get('_bound_account_id') and item.get('account_id') != row['_bound_account_id']:
                raise ValueError('NH에서 조회한 계좌와 저장할 계좌가 다릅니다. 원래 조회 계좌를 유지하세요.')
            previous = state[key].get(item['id'])
            if previous:
                if previous.get('deleted'):
                    stats['skipped'] += 1; continue
                if key == 'trades' and any(item.get(k) != previous.get(k) for k in ('account_id', 'instrument_id', 'side', 'executed_at')):
                    raise ValueError('같은 원본 ID에 다른 거래 정보가 있습니다. 중복 여부를 확인하세요.')
                if not update_existing or not item.get('external_id') or item.get('external_id') != previous.get('external_id'):
                    stats['skipped'] += 1; continue
                stats['updated'] += 1
            else:
                stats['events' if key == 'dividend_events' else 'added'] += 1
            state[key][item['id']] = item
    validate_state(state)
    return state, stats
