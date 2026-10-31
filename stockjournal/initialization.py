"""Portable personal initialization packages; never bundled with public code."""
import copy
import json
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal

from .broker_accounts import account_metadata
from .domain import empty_state, validate_state
from .importers import ImportPreview
from .workflow import stage_import, selectable, validate_ids, resolve_accounts

FORMAT='stockjournal-initialization'
FIELDS=('accounts','instruments','trades','dividends','dividend_events','issues')


def _json_default(value):
    if isinstance(value,(date,datetime)):
        return value.isoformat()
    if isinstance(value,Decimal):
        return format(value,'f')
    raise TypeError('초기 자료에 저장할 수 없는 원본 값이 있습니다.')


def make_package(preview, source):
    prepared=copy.deepcopy(preview)
    prepared.accounts=[account_metadata(row) for row in prepared.accounts]
    for key in ('trades','dividends','dividend_events'):
        for row in getattr(prepared,key):
            if row.get('_warnings'):
                row['import_notes']=copy.deepcopy(row['_warnings'])
    selected=selectable(prepared)
    state,stats=stage_import(empty_state(),prepared,selected)
    pending=ImportPreview(accounts=copy.deepcopy(prepared.accounts),instruments=copy.deepcopy(prepared.instruments),issues=copy.deepcopy(prepared.issues))
    for key in ('trades','dividends','dividend_events'):
        setattr(pending,key,[copy.deepcopy(row) for row in getattr(prepared,key) if row['id'] not in state[key]])
    package={'format':FORMAT,'version':1,'source':copy.deepcopy(source),'state':state,'pending':asdict(pending),'summary':{**stats,'accounts':len(state['accounts']),'instruments':len(state['instruments']),'pending_trades':len(pending.trades),'pending_dividends':len(pending.dividends),'pending_events':len(pending.dividend_events)}}
    return json.loads(json.dumps(package,ensure_ascii=False,allow_nan=False,default=_json_default))


def preview_from_dict(value):
    if not isinstance(value,dict) or set(value)!=set(FIELDS):
        raise ValueError('보류 자료의 형식을 확인하세요.')
    for key in FIELDS:
        if not isinstance(value[key],list) or any(not isinstance(row,str if key=='issues' else dict) for row in value[key]):
            raise ValueError('보류 자료 목록의 형식을 확인하세요.')
    preview=ImportPreview(**copy.deepcopy(value));validate_ids(preview)
    return preview


def read_package(package):
    if not isinstance(package,dict) or package.get('format')!=FORMAT or type(package.get('version')) is not int or package['version']!=1:
        raise ValueError('지원하는 초기 입력 파일이 아닙니다.')
    if set(package)!={'format','version','source','state','pending','summary'} or not isinstance(package['source'],dict) or not isinstance(package['summary'],dict):
        raise ValueError('초기 입력 파일의 필드를 확인하세요.')
    state=copy.deepcopy(package['state']);validate_state(state)
    return state,preview_from_dict(package['pending'])


def merge_package(current, package):
    imported,pending=read_package(package)
    accounts,mapping=resolve_accounts(current['accounts'],imported['accounts'].values())
    imported['accounts']={target:copy.deepcopy(accounts[target]) for target in mapping.values()}
    for key in ('trades','dividends'):
        for row in imported[key].values():
            row['account_id']=mapping.get(row['account_id'],row['account_id'])
    # Do not turn a re-import into an implicit rollback of edited records.
    for key in ('trades','dividends','dividend_events'):
        for uid,row in imported[key].items():
            old=current[key].get(uid)
            if old and not old.get('deleted'):
                comparable=lambda r:{k:v for k,v in r.items() if k not in ('updated_at','import_notes')}
                if comparable(old)!=comparable(row):
                    raise ValueError('기존 기록과 같은 ID의 값이 달라 초기 자료를 합치지 않았습니다. 기존 수정 내용을 확인하세요.')
    preview=ImportPreview(**{key:list(imported[key].values()) for key in FIELDS if key!='issues'})
    result,stats=stage_import(current,preview)
    return result,_canonical_pending(pending,result),stats


def _canonical_pending(preview, state):
    result=copy.deepcopy(preview)
    accounts,mapping=resolve_accounts(state['accounts'],result.accounts)
    result.accounts=[copy.deepcopy(accounts[target]) for target in dict.fromkeys(mapping.values())]
    for key in ('trades','dividends'):
        for row in getattr(result,key):
            row['account_id']=mapping.get(row.get('account_id'),row.get('account_id'))
    return result


def merge_pending(existing, incoming, state):
    """Retain reviewed local rows across repeated or different initial packages."""
    old,new=_canonical_pending(existing,state),_canonical_pending(incoming,state)
    result=ImportPreview(issues=list(dict.fromkeys([*old.issues,*new.issues])))
    for key in ('accounts','instruments'):
        rows={row['id']:copy.deepcopy(row) for row in getattr(new,key)}
        rows.update({row['id']:copy.deepcopy(row) for row in getattr(old,key)})
        setattr(result,key,list(rows.values()))
    for key in ('trades','dividends','dividend_events'):
        rows={row['id']:copy.deepcopy(row) for row in getattr(new,key)}
        rows.update({row['id']:copy.deepcopy(row) for row in getattr(old,key)})
        setattr(result,key,[row for uid,row in rows.items() if uid not in state[key]])
    validate_ids(result)
    return result
