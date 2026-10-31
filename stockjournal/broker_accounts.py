"""Account binding and broker snapshot reconciliation, without network or secrets."""
import copy
import hashlib
import re
from datetime import datetime
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from .domain import portfolio, validate_state, decimal_value
from .importers import ImportPreview
from .workflow import record, now


def is_nh_account(account):
    broker = str(account.get('broker', '')).replace(' ', '').upper()
    return account.get('connection_mode') == 'nh' or broker in ('NH', 'NH증권', 'NH투자증권', '나무', '나무증권', 'NAMUH')


def account_metadata(account):
    """Infer only explicit broker/type labels from legacy account aliases."""
    row = copy.deepcopy(account)
    name = row['name']
    if not row.get('broker'):
        for prefix, broker in (('NH증권_', 'NH투자증권'), ('삼성증권_', '삼성증권'), ('미래에셋_', '미래에셋증권')):
            if name.startswith(prefix):
                row['broker'] = broker
                break
    if row.get('kind') in ('', 'brokerage', None):
        row['kind'] = next((kind for kind in ('연금저축', 'IRP', 'ISA', 'DC') if name.endswith('_' + kind)), '일반')
    row.setdefault('connection_mode', 'nh' if is_nh_account(row) else 'manual')
    return row


def bind_account(state, discovered, *, existing_id=None, name=None, kind='일반', currency='KRW'):
    number = discovered.get('account_number', '')
    if not isinstance(number, str) or not re.fullmatch(r'[0-9]{11}', number):
        raise ValueError('공식 조회 결과의 계좌번호 형식을 확인하세요.')
    if not discovered.get('usable') or discovered.get('environment') != 'live' or discovered.get('account_type') not in ('01', '02'):
        raise ValueError('매매일지 연결에는 운영 계좌를 선택하세요. 모의 계좌와 구분합니다.')
    fingerprint = hashlib.sha256(('nh-account:' + number).encode()).hexdigest()
    matches = [r for r in state['accounts'].values() if not r.get('deleted') and r.get('broker_fingerprint') == fingerprint]
    if matches and existing_id and matches[0]['id'] != existing_id:
        raise ValueError('이 나무 계좌는 이미 다른 계좌 별칭에 연결되어 있습니다.')
    uid = existing_id or (matches[0]['id'] if matches else None)
    result = copy.deepcopy(state)
    if uid:
        account = result['accounts'].get(uid)
        if not account or account.get('deleted') or not is_nh_account(account):
            raise ValueError('기존 나무 계좌를 선택하세요. 다른 증권사 계좌는 수동 관리를 유지합니다.')
        old = account.get('broker_fingerprint')
        if old and old != fingerprint:
            raise ValueError('다른 실제 계좌에 연결된 별칭입니다. 새 계좌로 등록하세요.')
    else:
        name = (name or ('나무 ' + number[-4:])).strip()
        if not name or any(not a.get('deleted') and a['name'] == name for a in state['accounts'].values()):
            raise ValueError('구분되는 새 계좌 별칭을 입력하세요.')
        account = record(name=name,broker='NH투자증권',kind=kind,currency=currency)
        uid=account['id'];result['accounts'][uid]=account
    account.update(broker='NH투자증권',connection_mode='nh',broker_fingerprint=fingerprint,updated_at=now())
    validate_state(result)
    return result, uid


def _account(state, account_id):
    row = state['accounts'].get(account_id)
    if not row or row.get('deleted') or not is_nh_account(row):
        raise ValueError('나무 계좌를 선택하세요. 다른 증권사 계좌는 수동으로 입력합니다.')
    return row


def _decimal(value):
    return decimal_value(str(value), 'NH 잔고 수치')


def holdings_comparison(state, account_id, snapshot):
    _account(state, account_id)
    journal = {r['instrument_id']: r for r in portfolio(state, account_id=account_id)['rows']}
    grouped = {}
    for row in snapshot['holdings']:
        uid = row['instrument_id']
        item=grouped.setdefault(uid, {'instrument_id':uid,'name':row['name'],'currency':row['currency'],
                                     'broker_quantity':Decimal(0),'rows':[]})
        item['broker_quantity'] += _decimal(row['quantity']);item['rows'].append(row)
    markets={r['market'] for r in snapshot['holdings']}
    if snapshot.get('market'):
        markets={snapshot['market']}
    for uid, row in journal.items():
        if row['quantity'] and (not markets or uid.split(':',1)[0] in markets):
            instrument=state['instruments'][uid]
            grouped.setdefault(uid,{'instrument_id':uid,'name':instrument['name'],'currency':row['currency'],'broker_quantity':Decimal(0),'rows':[]})
    result=[]
    for uid, item in sorted(grouped.items()):
        item['journal_quantity']=journal.get(uid,{}).get('quantity',Decimal(0))
        item['difference']=item['broker_quantity']-item['journal_quantity']
        item['opening_allowed']=item['broker_quantity']>0 and not any(not r.get('deleted') and r['account_id']==account_id and r['instrument_id']==uid for r in state['trades'].values())
        result.append(item)
    return result


def opening_preview(state, account_id, snapshot, selected, *, cash_confirmed=False):
    _account(state, account_id)
    stamp=snapshot.get('as_of','')
    when=datetime.fromisoformat(stamp.replace('Z','+00:00'))
    if when.tzinfo is None:
        raise ValueError('잔고 조회 시각의 시간대를 확인하세요.')
    preview=ImportPreview()
    comparisons={r['instrument_id']:r for r in holdings_comparison(state,account_id,snapshot)}
    for uid in sorted(selected):
        item=comparisons.get(uid)
        if not item or not item['opening_allowed']:
            raise ValueError('거래 이력이 있는 종목에는 기초 보유를 더하지 않습니다. 수량 차이를 먼저 확인하세요.')
        rows=item['rows']
        for row in rows:
            if row.get('loan_date') or _decimal(row.get('loan_balance') or '0')>0:
                raise ValueError('대출·신용 잔고는 기초 보유로 변환하지 않습니다.')
            if not row.get('cash_position') and not cash_confirmed:
                raise ValueError('현금 보유이며 신용·대출 잔고가 아닌지 확인해 주세요.')
        quantity=item['broker_quantity']
        with localcontext() as ctx:
            ctx.prec=50
            cost=sum((_decimal(r['book_value']) if r.get('book_value') is not None else _decimal(r['quantity'])*_decimal(r['average_price']) for r in rows),Decimal(0))
            if cost<=0:
                raise ValueError('매입원가를 확인할 수 없습니다. 취득 단가를 직접 입력하세요.')
            price=format(cost/quantity,'f')
        source=rows[0]
        if uid not in state['instruments']:
            preview.instruments.append(record(id=uid,market=source['market'],code=source['code'],name=source['name'],currency=source['currency'],asset_type='ETF' if source['code'] in ('TQQQ','SPYM','SGOV') else '주식',sector=''))
        identifier='opening:'+hashlib.sha256((account_id+'|'+uid+'|'+stamp).encode()).hexdigest()[:32]
        preview.trades.append(record(id=identifier,account_id=account_id,instrument_id=uid,currency=source['currency'],executed_at=stamp,side='OPENING',quantity=format(quantity,'f'),price=price,fee='0',tax='0',source='nh:opening-balance',external_id=identifier,opening_snapshot_at=stamp,_bound_account_id=account_id,_blocked=False,_warnings=['조회 시점의 기초 보유입니다. 이 날짜 이전 체결을 추가하면 중복되므로 먼저 대조하세요.']))
    preview.issues=['기초 보유는 과거 실현손익을 복구하지 않습니다. 취득원가·보유량을 확인한 뒤 저장하세요.']
    return preview


def _date(row):
    value=datetime.fromisoformat(row['executed_at'].replace('Z','+00:00'))
    if value.tzinfo:
        value=value.astimezone(ZoneInfo('America/New_York' if row['instrument_id'].startswith('US:') else 'Asia/Seoul'))
    return value.date()


def broker_import_preview(state, account_id, rows):
    _account(state, account_id)
    preview=ImportPreview();instruments={}
    existing=[r for r in state['trades'].values() if not r.get('deleted') and r['account_id']==account_id]
    for original in rows:
        row=copy.deepcopy(original);row['account_id']=account_id;row['_bound_account_id']=account_id;row.setdefault('_warnings',[]);row.setdefault('_blocked',False)
        uid=row['instrument_id'];market,code=uid.split(':',1)
        if uid not in state['instruments']:
            instruments[uid]=record(id=uid,market=market,code=code,name=row.get('_instrument_name') or code,currency=row.get('_instrument_currency') or row['currency'],asset_type='ETF' if code in ('TQQQ','SPYM','SGOV') else '주식',sector='')
        for old in existing:
            if old['id']==row['id'] or old['instrument_id']!=uid:
                continue
            before_opening=old['side']=='OPENING' and _date(row)<=_date(old)
            nh_source=old.get('source')=='NH' or old.get('source','').startswith('nh:')
            other_source=(not nh_source and old['side']==row['side'] and _date(old)==_date(row))
            if before_opening or other_source:
                row['_blocked']=True
                row['_warnings'].append('기초 보유 이전 체결과 중복될 수 있습니다. 보유 기준일을 확인하세요.' if before_opening else '같은 날짜·종목·방향의 기존 수동/엑셀 거래가 있어 중복 검토가 필요합니다.')
                break
        preview.trades.append(row)
    preview.instruments=list(instruments.values())
    preview.issues=['주문별 누적 체결입니다. 반복 조회는 갱신 허용을 선택하고 비용·처리 순서를 확인하세요.', '기존 수동/엑셀 기록과 중복 가능성이 있는 행은 검토 후 저장합니다.']
    return preview
