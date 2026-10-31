"""Windows native trading journal. Network work never calls Tk from threads."""
import argparse
import copy
import csv
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
from decimal import Decimal
from datetime import datetime, timezone, timedelta
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import webbrowser

from . import __version__
from .domain import empty_state, portfolio, validate_state
from .store import JournalStore
from .workflow import record, now, unchanged, account_choices, clean, stage_import, validate_ids, selectable, groups
from .ui import Table, FormDialog, money, number


def today():
    return datetime.now(timezone(timedelta(hours=9))).date().isoformat()


def data_directory():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local' / 'share'))) / 'StockJournal'


def read_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else copy.deepcopy(default)


def write_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


class JournalApp:
    def __init__(self, root, directory=None, smoke=False):
        from .secrets import SecretStore
        from .session import InstanceLock
        self.root = root; self.directory = Path(directory or data_directory()); self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = InstanceLock(self.directory / '.instance.lock')
        self.store = JournalStore(self.directory / 'journal.sqlite3'); self.state = self.store.load()
        self.settings = read_json(self.directory / 'settings.json', {}); self.quotes = read_json(self.directory / 'quotes.json', {})
        self.secrets = SecretStore(self.directory / 'secrets.bin')
        self.smoke = smoke; self.busy = False; self.queue = queue.Queue(); self.preview = None; self.chosen = set(); self.history_filter = None
        self.last_quote = 0; self.pending_sync = None
        root.title(f'주식 매매일지 · Stock Journal {__version__}')
        root.geometry(f"{min(1380,root.winfo_screenwidth()-24)}x{min(860,root.winfo_screenheight()-90)}")
        root.minsize(1000, 660)
        style = ttk.Style(root); style.theme_use('clam')
        style.configure('.', font=('Malgun Gothic', 10)); style.configure('TFrame', background='#f4f7fa')
        style.configure('TLabel', background='#f4f7fa'); style.configure('Treeview', rowheight=30, background='white', fieldbackground='white')
        style.configure('Treeview.Heading', font=('Malgun Gothic', 10, 'bold'), padding=7)
        style.configure('Primary.TButton', background='#116c68', foreground='white', padding=8)
        header = ttk.Frame(root, padding=(20, 15)); header.pack(fill='x')
        ttk.Label(header, text='나의 투자 기록', font=('Malgun Gothic', 21, 'bold')).pack(side='left')
        self.account = tk.StringVar(value='전체 계좌'); self.account_box = ttk.Combobox(header, textvariable=self.account, state='readonly', width=28)
        self.account_box.pack(side='right'); self.account_box.bind('<<ComboboxSelected>>', lambda e: self.refresh())
        ttk.Button(header, text='동기화', command=self.sync).pack(side='right', padx=8)
        ttk.Button(header, text='시세 갱신', command=self.refresh_quotes).pack(side='right')
        self.tabs = ttk.Notebook(root); self.tabs.pack(fill='both', expand=True, padx=18)
        self.pages = {}
        for key, label in [('dashboard', '한눈에 보기'), ('trades', '매매 기록'), ('imports', '일괄 가져오기'), ('dividends', '배당 관리'), ('instruments', '종목 관리'), ('settings', '설정 · 계좌')]:
            page = ttk.Frame(self.tabs, padding=14); self.pages[key] = page; self.tabs.add(page, text='  '+label+'  ')
        self.build_dashboard(); self.build_trades(); self.build_imports(); self.build_dividends(); self.build_instruments(); self.build_settings()
        self.status = tk.StringVar(value='이 PC에 저장합니다. 다른 PC에서 보려면 비공개 기록 저장소를 연결하세요.')
        self.tabs.pack_forget()
        footer = ttk.Frame(root, padding=12); footer.pack(side='bottom',fill='x')
        ttk.Label(footer, textvariable=self.status).pack(side='left'); self.progress = ttk.Progressbar(footer, mode='indeterminate', length=120); self.progress.pack(side='right')
        self.tabs.pack(fill='both',expand=True,padx=18)
        root.protocol('WM_DELETE_WINDOW', self.close); self.refresh(); root.after(100, self.poll); root.after(15000, self.tick)
        if not smoke and self.settings.get('auto_sync'):
            root.after(1000, self.sync)

    def actions(self, page, buttons):
        bar = ttk.Frame(page); bar.pack(fill='x', pady=(0, 12))
        for label, command in buttons:
            ttk.Button(bar, text=label, command=lambda c=command: self.safe(c)).pack(side='left', padx=(0, 7))
        return bar

    def safe(self, function):
        try:
            function()
        except Exception as exc:
            messagebox.showerror('확인 필요', str(exc), parent=self.root)

    def active(self, key, state=None):
        return [r for r in (state or self.state)[key].values() if not r.get('deleted')]

    def account_id(self):
        return account_choices(self.state).get(self.account.get())

    def instrument_choices(self, state=None):
        return {f"{r['name']} ({r['id']})": r['id'] for r in self.active('instruments', state)}

    def idle(self):
        if self.busy:
            raise ValueError('조회·동기화가 끝난 뒤 저장해 주세요.')

    def backup(self):
        return self.store.backup(self.directory / 'backups' / (datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.sqlite3'))

    def save(self, state, expected=None, autosync=True):
        self.idle()
        if expected is not None and self.state != expected:
            raise ValueError('검토 중 원장이 변경되었습니다. 최신 내용을 다시 확인하세요.')
        validate_state(state)
        if state != self.state:
            self.backup(); self.store.save(state); self.state = self.store.load()
            self.settings['dirty'] = True; write_json(self.directory / 'settings.json', self.settings)
        self.refresh(); self.status.set('기록을 이 PC에 저장했습니다.')
        if autosync and not self.smoke and self.settings.get('auto_sync'):
            if self.pending_sync:
                self.root.after_cancel(self.pending_sync)
            self.pending_sync = self.root.after(800, self.sync)

    def work(self, label, function, callback, error=None):
        self.idle(); self.busy = True; self.status.set(label); self.progress.start()
        def run():
            try:
                result = function(); self.queue.put((True, result, callback, error))
            except Exception as exc:
                self.queue.put((False, exc, callback, error))
        threading.Thread(target=run, daemon=True).start()

    def poll(self):
        try:
            ok, value, done, error = self.queue.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False; self.progress.stop()
            def finish():
                if ok:
                    done(value)
                elif error:
                    error(value)
                else:
                    self.status.set('작업 실패: 기존 기록을 유지합니다.'); raise value
            self.safe(finish)
        self.root.after(100, self.poll)

    def build_dashboard(self):
        page = self.pages['dashboard']
        self.actions(page, [('+ 매수 입력', lambda: self.edit('trades')), ('매도 입력', lambda: self.edit('trades', defaults={'side': 'SELL'})), ('전체 이력', self.clear_history)])
        self.totals = tk.StringVar(); ttk.Label(page, textvariable=self.totals, font=('Malgun Gothic', 12), wraplength=1250).pack(fill='x', pady=(0, 16))
        self.holdings = Table(page, [('account','계좌',120),('name','종목',210),('currency','통화',60),('quantity','보유량',90),('cost','매입원가',120),('value','평가금액',120),('unrealized','평가손익',120),('realized','누적 실현손익',120),('dividend','받은 배당',110),('total','총손익',120)])
        self.holdings.pack(fill='both', expand=True); self.holdings.tree.bind('<Double-1>', self.open_history)
        ttk.Label(page, text='매도 완료 종목도 표시합니다. 수수료·세금을 반영하며, 통화별로 계산합니다. 시세 수신 시각은 종목 화면에서 확인하세요.', foreground='#536471').pack(anchor='w', pady=10)

    def build_trades(self):
        page = self.pages['trades']
        self.actions(page, [('+ 매수', lambda:self.edit('trades')), ('매도', lambda:self.edit('trades',defaults={'side':'SELL'})), ('선택 수정',lambda:self.edit_selected('trades',self.trades)), ('삭제',lambda:self.delete('trades',self.trades)), ('NH 체결 조회',self.import_nh), ('CSV 내보내기',self.export_csv), ('종목 필터 해제',self.clear_history)])
        self.trades = Table(page, [('date','체결일',160),('account','계좌',120),('side','구분',70),('name','종목',220),('quantity','수량',85),('price','단가',110),('fee','수수료',90),('tax','세금',80),('memo','메모',230)])
        self.trades.pack(fill='both',expand=True); self.trades.tree.bind('<Double-1>',lambda e:self.edit_selected('trades',self.trades))

    def build_dividends(self):
        page = self.pages['dividends']
        self.actions(page,[('수령 추가',lambda:self.edit('dividends')),('수령 수정',lambda:self.edit_selected('dividends',self.receipts)),('수령 삭제',lambda:self.delete('dividends',self.receipts)),('ETFcheck 열기',lambda:webbrowser.open('https://www.etfcheck.co.kr/'))])
        self.receipts=Table(page,[('date','수령일',130),('account','계좌',140),('name','종목',220),('gross','세전 금액',140),('tax','세금',110),('net','순수령액',140),('currency','통화',70)],height=4);self.receipts.pack(fill='both',expand=True)
        ttk.Label(page,text='참고·공시 일정 · 선택 계좌의 보유 종목만 표시하며, 실제 받은 배당에 합산하지 않습니다.',font=('Malgun Gothic',11,'bold')).pack(anchor='w',pady=10)
        self.actions(page,[('일정 추가',lambda:self.edit('dividend_events')),('일정 수정',lambda:self.edit_selected('dividend_events',self.events)),('일정 삭제',lambda:self.delete('dividend_events',self.events))])
        self.events=Table(page,[('name','종목',220),('record','기준일',120),('pay','지급 예정일',120),('amount','주당 금액',120),('currency','통화',70),('status','구분',90),('source','출처',250)],height=4);self.events.pack(fill='both',expand=True)

    def build_instruments(self):
        page=self.pages['instruments'];bar=self.actions(page,[('종목 추가',lambda:self.edit('instruments')),('수정',lambda:self.edit_selected('instruments',self.instruments)),('공식 종목 목록 받기',self.load_master),('가격 직접 입력',self.manual_quote)])
        self.search=tk.StringVar();ttk.Entry(bar,textvariable=self.search,width=30).pack(side='right');self.search.trace_add('write',lambda *a:self.render_instruments())
        ttk.Label(bar,text='이름·코드 검색').pack(side='right',padx=6)
        self.instruments=Table(page,[('market','시장',70),('code','코드',100),('name','종목명',260),('currency','통화',70),('type','종류',90),('price','현재가',120),('asof','수신 시각',220),('source','가격 출처',180)])
        self.instruments.pack(fill='both',expand=True)

    def build_settings(self):
        page=self.pages['settings'];self.actions(page,[('계좌 추가',lambda:self.edit('accounts')),('계좌 수정',lambda:self.edit_selected('accounts',self.accounts)),('기록 백업 내보내기',self.export_backup),('백업 복원',self.restore_backup)])
        self.accounts=Table(page,[('name','계좌 별칭',240),('broker','증권사',180),('kind','계좌 종류',130),('currency','기준 통화',100)],height=2);self.accounts.pack(fill='x',pady=(0,12))
        panel=ttk.Notebook(page);panel.pack(fill='both',expand=True);self.connection_tabs=panel;self.settings_buttons=[]
        nh=ttk.Frame(panel,padding=15);gh=ttk.Frame(panel,padding=15);panel.add(nh,text='NH 연결');panel.add(gh,text='다른 PC와 동기화')
        self.setting_vars={}
        fields=[(nh,'nh_app_key','NH 앱키',True),(nh,'nh_app_secret','NH 앱시크릿',True),(nh,'quote_interval','시세 갱신 간격(초)',False),(gh,'sync_repo','비공개 기록 저장소 (사용자/저장소)',False),(gh,'sync_branch','브랜치',False),(gh,'github_token','GitHub 토큰',True)]
        counts={nh:0,gh:0}
        for parent,key,label,secret in fields:
            row=counts[parent];counts[parent]+=1
            value=self.secrets.get(key) if secret else self.settings.get(key,'60' if key=='quote_interval' else 'main' if key=='sync_branch' else '')
            var=tk.StringVar(value=str(value));self.setting_vars[key]=var
            ttk.Label(parent,text=label).grid(row=row,column=0,sticky='w',padx=(0,16),pady=6)
            ttk.Entry(parent,textvariable=var,show='•' if secret else '',width=65).grid(row=row,column=1,sticky='ew',pady=6);parent.columnconfigure(1,weight=1)
        self.auto_quotes=tk.BooleanVar(value=self.settings.get('auto_quotes',False));self.auto_sync=tk.BooleanVar(value=self.settings.get('auto_sync',False))
        ttk.Checkbutton(nh,text='앱 실행 중 국내 보유 종목 현재가 자동 갱신',variable=self.auto_quotes).grid(row=3,column=0,columnspan=2,sticky='w',pady=8)
        ttk.Checkbutton(gh,text='실행 시 및 기록 저장 후 자동 동기화',variable=self.auto_sync).grid(row=3,column=0,columnspan=2,sticky='w',pady=8)
        for parent,text in [(nh,'나무 PLUG 운영 키를 사용합니다. 30초 이상 간격의 현재가 조회입니다.\n해외 종목 가격은 직접 입력할 수 있습니다. 인증값은 이 PC에만 저장합니다.'),(gh,'별도 Private 저장소를 만들어 연결하세요. 공개 저장소에는 기록을 올리지 않습니다.\n토큰은 해당 저장소의 Contents 읽기/쓰기 권한이 필요합니다. 다른 PC에도 키를 각각 입력하세요.')]:
            ttk.Label(parent,text=text,wraplength=1000,foreground='#536471').grid(row=4,column=0,columnspan=2,sticky='w',pady=12)
            button=ttk.Button(parent,text='설정 저장',command=lambda:self.safe(self.save_settings),style='Primary.TButton');button.grid(row=5,column=0,sticky='w');self.settings_buttons.append(button)
        ttk.Button(gh,text='지금 동기화',command=lambda:self.safe(self.sync)).grid(row=5,column=1,sticky='w')
        ttk.Label(page,text='기록 폴더: '+str(self.directory),foreground='#536471').pack(anchor='w',pady=10)

    def edit_selected(self, collection, table):
        uid=table.selected()
        if uid:
            self.edit(collection,self.state[collection][uid])

    def edit(self, collection, existing=None, defaults=None, preview_callback=None, preview_state=None):
        opened=copy.deepcopy(existing or {}); values={**(defaults or {}),**opened}; state=preview_state or self.state
        accounts=account_choices(state); instruments=self.instrument_choices(state)
        currencies={c:c for c in ('KRW','USD','JPY','EUR','HKD')}
        schemas={
            'accounts':[('name','계좌 별칭','',None),('broker','증권사','',None),('kind','계좌 종류','일반',{v:v for v in ('일반','ISA','연금저축','IRP','DC','기타')}),('currency','기준 통화','KRW',currencies)],
            'instruments':[('market','상장 시장','KR',{'한국':'KR','미국':'US','일본':'JP','홍콩':'HK'}),('code','종목 코드','',None),('name','종목명','',None),('currency','거래 통화','KRW',currencies),('asset_type','종류','주식',{v:v for v in ('주식','ETF','ETN','기타')}),('sector','분류','',None)],
            'trades':[('account_id','계좌',self.account_id() or next(iter(accounts.values()),''),accounts),('instrument_id','종목 (이름·코드 검색)','',instruments),('executed_at','체결일 / 시간',today(),None),('side','거래 구분','BUY',{'매수':'BUY','매도':'SELL','기초 보유':'OPENING'}),('quantity','수량','',None),('price','단가','',None),('fee','수수료 금액','0',None),('tax','세금 금액','0',None),('sequence','같은 시각의 처리 순서',max((r.get('sequence',0) for r in self.active('trades')),default=0)+1,None),('memo','메모','',None)],
            'dividends':[('account_id','수령 계좌',self.account_id() or next(iter(accounts.values()),''),accounts),('instrument_id','종목','',instruments),('paid_date','실제 수령일',today(),None),('gross','세전 수령액','',None),('tax','세금','0',None),('memo','메모','',None)],
            'dividend_events':[('instrument_id','종목','',instruments),('record_date','기준일 (선택)','',None),('pay_date','지급 예정일 (선택)','',None),('amount_per_share','주당 배당금','',None),('status','구분','reference',{'참고':'reference','공시':'announced'}),('source','출처','',None),('memo','메모','',None)]}
        if collection in ('trades','dividends','dividend_events') and (not instruments or (collection!='dividend_events' and not accounts)):
            raise ValueError('먼저 계좌와 종목을 추가하거나 엑셀을 가져오세요.')
        fields=[(k,label,values.get(k,default),choices) for k,label,default,choices in schemas[collection]]
        def done(changes):
            self.idle()
            if not preview_callback:
                unchanged(self.state,collection,opened)
            for key in ('quantity','price','fee','tax','gross','amount_per_share'):
                if key in changes:
                    changes[key]=number(changes[key])
                    if Decimal(changes[key])<0 or (key=='quantity' and Decimal(changes[key])==0):
                        raise ValueError('수량은 양수, 금액은 0 이상으로 입력하세요.')
            if 'sequence' in changes:
                changes['sequence']=int(changes['sequence'])
            for key in ('executed_at','paid_date','record_date','pay_date'):
                if key in changes and (changes[key] or key in ('executed_at','paid_date')):
                    datetime.fromisoformat(changes[key].replace('Z','+00:00'))
            if 'instrument_id' in changes:
                changes['currency']=state['instruments'][changes['instrument_id']]['currency']
            item=record(**{**opened,**changes,'updated_at':now()})
            if collection=='accounts':
                if not item['name']:
                    raise ValueError('계좌 별칭을 입력하세요.')
                if any(r['name']==item['name'] and r['id']!=item['id'] for r in self.active('accounts')):
                    raise ValueError('같은 계좌 별칭이 있습니다. 구분되는 이름을 입력하세요.')
            if collection=='instruments':
                item['code']=item['code'].upper();item['id']=f"{item['market']}:{item['code']}"
                if opened and item['id']!=opened['id']:
                    raise ValueError('기존 종목 코드는 바꿀 수 없습니다. 새 종목을 추가하세요.')
                if not opened and item['id'] in self.state['instruments']:
                    raise ValueError('이미 등록된 종목 코드입니다. 수정 메뉴를 사용하세요.')
                if opened and item['currency']!=opened['currency'] and any(r.get('instrument_id')==item['id'] for key in ('trades','dividends','dividend_events') for r in self.state[key].values()):
                    raise ValueError('기록이 연결된 종목의 통화를 변경할 수 없습니다.')
            if preview_callback:
                preview_callback(item);return
            target=copy.deepcopy(self.state);target[collection][item['id']]=clean(item);self.save(target)
        FormDialog(self.root,'기록 수정' if existing else '새 기록 입력',fields,done,'날짜는 YYYY-MM-DD 또는 YYYY-MM-DDTHH:MM:SS. 국내 상장 해외지수 ETF는 한국/KRW입니다. 배당 일정은 실제 수령액에 합산되지 않습니다.')

    def delete(self,collection,table):
        self.idle();uid=table.selected()
        if not uid:
            return
        expected=copy.deepcopy(self.state)
        if messagebox.askyesno('기록 삭제','선택한 기록을 삭제할까요? 변경 전 자동 백업을 남깁니다.',parent=self.root):
            target=copy.deepcopy(expected);target[collection][uid].update(deleted=True,updated_at=now());self.save(target,expected=expected)

    def refresh(self):
        choices=account_choices(self.state);self.account_box.configure(values=['전체 계좌',*choices])
        if self.account.get() not in choices:
            self.account.set('전체 계좌')
        selected=self.account_id();result=portfolio(self.state,self.quotes,selected);self.portfolio_rows={}
        self.totals.set('\n'.join(f"{currency}   매입원가 {money(total['cost'])}   평가금액 {money(total['market_value'])}   실현손익 {money(total['realized_pnl'])}   평가손익 {money(total['unrealized_pnl'])}   배당 {money(total['dividends_net'])}   총손익 {money(total['total_pnl'])}" for currency,total in result['totals'].items()) or '계좌와 종목을 등록하고 첫 거래를 입력하세요. 기존 엑셀을 가져올 수도 있습니다.')
        self.holdings.clear()
        for index,row in enumerate(result['rows']):
            uid=str(index);self.portfolio_rows[uid]=row
            self.holdings.tree.insert('', 'end',iid=uid,values=(self.state['accounts'][row['account_id']]['name'],self.state['instruments'][row['instrument_id']]['name'],row['currency'],money(row['quantity'],6),*[money(row[key]) for key in ('cost','market_value','unrealized_pnl','realized_pnl','dividends_net','total_pnl')]))
        self.trades.clear()
        for row in sorted(self.active('trades'),key=lambda r:(r['executed_at'],r.get('sequence',0)),reverse=True):
            if selected and row['account_id']!=selected or self.history_filter and row['instrument_id']!=self.history_filter:
                continue
            self.trades.tree.insert('','end',iid=row['id'],values=(row['executed_at'],self.state['accounts'][row['account_id']]['name'],{'BUY':'매수','SELL':'매도','OPENING':'기초'}[row['side']],self.state['instruments'][row['instrument_id']]['name'],money(row['quantity'],6),money(row['price'],4),money(row['fee']),money(row['tax']),row.get('memo','')))
        self.accounts.clear()
        for row in self.active('accounts'):
            self.accounts.tree.insert('','end',iid=row['id'],values=tuple(row.get(k,'') for k in ('name','broker','kind','currency')))
        self.receipts.clear();self.events.clear()
        from decimal import Decimal
        for row in self.active('dividends'):
            if selected and row['account_id']!=selected:
                continue
            self.receipts.tree.insert('','end',iid=row['id'],values=(row['paid_date'],self.state['accounts'][row['account_id']]['name'],self.state['instruments'][row['instrument_id']]['name'],money(row['gross']),money(row['tax']),money(Decimal(row['gross'])-Decimal(row['tax'])),row['currency']))
        held={row['instrument_id'] for row in result['rows'] if row['quantity']>0}
        for row in self.active('dividend_events'):
            if row['instrument_id'] not in held:
                continue
            self.events.tree.insert('','end',iid=row['id'],values=(self.state['instruments'][row['instrument_id']]['name'],row.get('record_date',''),row.get('pay_date',''),money(row['amount_per_share'],4),row['currency'],'공시' if row['status']=='announced' else '참고',row.get('source','')))
        self.render_instruments();self.import_account_box.configure(values=['원본 계좌 유지',*choices])
        if self.import_account.get() not in choices:
            self.import_account.set('원본 계좌 유지')

    def render_instruments(self):
        if not hasattr(self,'instruments'):
            return
        self.instruments.clear();query=self.search.get().casefold()
        for row in self.active('instruments'):
            if query and query not in (row['name']+' '+row['code']).casefold():
                continue
            quote=self.quotes.get(row['id'],{})
            self.instruments.tree.insert('','end',iid=row['id'],values=(row['market'],row['code'],row['name'],row['currency'],row.get('asset_type',''),money(quote.get('price')),quote.get('as_of',''),quote.get('source','')))

    def clear_history(self):
        self.history_filter=None;self.refresh()

    def open_history(self,event=None):
        uid=self.holdings.selected()
        if uid:
            row=self.portfolio_rows[uid];self.history_filter=row['instrument_id']
            self.account.set(next(k for k,v in account_choices(self.state).items() if v==row['account_id']));self.refresh();self.tabs.select(self.pages['trades'])

    def manual_quote(self):
        uid=self.instruments.selected()
        if not uid:
            return
        def done(values):
            from decimal import Decimal
            price=number(values['price'])
            if Decimal(price)<=0:
                raise ValueError('가격은 0보다 커야 합니다.')
            self.quotes[uid]={'price':price,'as_of':now(),'source':'사용자 입력'}
            write_json(self.directory/'quotes.json',self.quotes);self.refresh()
        FormDialog(self.root,'가격 직접 입력',[('price',self.state['instruments'][uid]['currency']+' 현재가',self.quotes.get(uid,{}).get('price',''),None)],done,'입력 가격은 이 PC의 평가용 캐시에 저장합니다. 실제 체결 내역을 변경하지 않습니다.')

    def build_imports(self):
        page=self.pages['imports'];bar=self.actions(page,[('엑셀 / CSV 열기',self.import_file),('이미지 OCR',self.import_image),('NH 체결 조회',self.import_nh)])
        self.import_account=tk.StringVar(value='원본 계좌 유지');self.import_account_box=ttk.Combobox(bar,textvariable=self.import_account,state='readonly',width=28);self.import_account_box.pack(side='right')
        ttk.Label(bar,text='저장할 계좌').pack(side='right',padx=8)
        source=ttk.LabelFrame(page,text='카톡·체결 텍스트',padding=8);source.pack(fill='x',pady=(0,10))
        self.text_input=tk.Text(source,height=2,wrap='word',font=('Malgun Gothic',10));self.text_input.pack(side='left',fill='x',expand=True)
        side=ttk.Frame(source);side.pack(side='right',padx=10);self.default_date=tk.StringVar(value=today())
        ttk.Label(side,text='날짜 누락 시 적용').pack();ttk.Entry(side,textvariable=self.default_date,width=15).pack(pady=4);ttk.Button(side,text='텍스트 분석',command=lambda:self.safe(self.import_text)).pack()
        self.import_summary=tk.StringVar(value='가져온 기록은 검토 후 저장합니다.');ttk.Label(page,textvariable=self.import_summary,wraplength=1200).pack(anchor='w',pady=6)
        self.import_table=Table(page,[('choose','저장',55),('type','유형',100),('date','날짜',130),('account','계좌',110),('name','종목',210),('quantity','수량 / 세전액',110),('price','단가 / 주당액',110),('notice','검토 사항',400)],height=4);self.import_table.pack(fill='both',expand=True)
        self.import_table.tree.bind('<Double-1>',lambda e:self.edit_import_row());self.import_table.tree.bind('<Button-1>',self.toggle_import)
        self.import_issues=tk.Text(page,height=2,wrap='word',font=('Malgun Gothic',9));self.import_issues.pack(fill='x',pady=8)
        self.import_buttons=self.actions(page,[('문제없는 거래 선택',self.select_valid),('거래 모두 해제',self.select_none),('선택 행 수정',self.edit_import_row),('선택 배당 제외',self.exclude_import_event),('검토한 기록 저장',self.commit_import)])
        self.include_events=tk.BooleanVar(value=True);self.update_orders=tk.BooleanVar(value=False)
        ttk.Checkbutton(page,text='참고 배당 일정도 가져오기',variable=self.include_events).pack(side='left')
        ttk.Checkbutton(page,text='같은 외부 거래 갱신 허용 (NH 주문별 누적체결, 검토 후 선택)',variable=self.update_orders).pack(side='left',padx=15)

    def load_preview(self,preview):
        validate_ids(preview);self.preview=preview;self.chosen=selectable(preview);self.update_orders.set(False)
        self.render_preview();self.tabs.select(self.pages['imports']);self.status.set('검토 대기: 아직 원장에 저장하지 않았습니다.')

    def preview_state(self):
        state=copy.deepcopy(self.state)
        if self.preview:
            for key in ('accounts','instruments'):
                for row in getattr(self.preview,key):
                    state[key].setdefault(row['id'],record(**clean(row)))
        return state

    def render_preview(self):
        if not self.preview:
            return
        self.import_table.clear();state=self.preview_state();blocked=0
        for key,label in [('trades','거래'),('dividends','배당 수령'),('dividend_events','참고 일정')]:
            for row in getattr(self.preview,key):
                uid=key+'/'+row['id'];is_blocked=bool(row.get('_blocked'));blocked+=is_blocked
                chosen=row['id'] in self.chosen if key=='trades' else not is_blocked
                instrument=state['instruments'].get(row.get('instrument_id'),{});account=state['accounts'].get(row.get('account_id'),{})
                notice=('기존 기록 / ' if row['id'] in self.state[key] else '')+' / '.join(row.get('_warnings',[]))
                self.import_table.tree.insert('','end',iid=uid,values=('✓' if chosen else '',{'BUY':'매수','SELL':'매도','OPENING':'기초'}.get(row.get('side'),label),row.get('executed_at') or row.get('paid_date') or row.get('record_date') or row.get('pay_date',''),account.get('name',''),instrument.get('name',row.get('instrument_id','종목 확인 필요')),money(row.get('quantity',row.get('gross','')),6),money(row.get('price',row.get('amount_per_share','')),4),notice),tags=('blocked',) if is_blocked else ())
        self.import_summary.set(f'거래 {len(self.preview.trades):,}건 중 선택 {len(self.chosen):,}건 · 실제 배당 {len(self.preview.dividends):,}건 · 참고 일정 {len(self.preview.dividend_events):,}건 · 수정 필요 {blocked:,}건')
        self.import_issues.configure(state='normal');self.import_issues.delete('1.0','end');self.import_issues.insert('1.0','\n'.join(self.preview.issues) or '각 거래의 날짜·계좌·금액·통화를 확인하세요.');self.import_issues.configure(state='disabled')

    def import_file(self):
        from .importers import preview_workbook,preview_csv
        path=filedialog.askopenfilename(parent=self.root,filetypes=[('엑셀 / CSV','*.xlsx *.csv *.tsv')])
        if path:
            parser=preview_workbook if path.lower().endswith('.xlsx') else preview_csv
            self.work('파일을 읽고 있습니다…',lambda:parser(path),self.load_preview)

    def import_text(self):
        from .importers import preview_text
        self.load_preview(preview_text(self.text_input.get('1.0','end'),default_account='' if self.import_account.get()=='원본 계좌 유지' else self.import_account.get(),default_date=self.default_date.get()))

    def import_image(self):
        from .ocr import extract_text
        path=filedialog.askopenfilename(parent=self.root,filetypes=[('체결 화면 이미지','*.png *.jpg *.jpeg *.bmp *.tif *.tiff')])
        if not path:
            return
        def done(text):
            self.text_input.delete('1.0','end');self.text_input.insert('1.0',text);self.import_text()
        self.work('Windows 내장 OCR로 이미지를 읽습니다…',lambda:extract_text(path),done)

    def select_valid(self):
        if self.preview:
            self.chosen=selectable(self.preview);self.render_preview()

    def select_none(self):
        self.chosen.clear();self.render_preview()

    def toggle_import(self,event):
        if not self.preview or self.import_table.tree.identify_column(event.x)!='#1':
            return
        item=self.import_table.tree.identify_row(event.y)
        if not item or not item.startswith('trades/'):
            return
        uid=item.split('/',1)[1];row=next(r for r in self.preview.trades if r['id']==uid)
        pair=next((g for g in groups(self.preview) if any(r['id']==uid for r in g)),[row])
        if any(r.get('_blocked') for r in pair):
            messagebox.showinfo('검토 필요','매수·매도의 연결된 행을 모두 수정한 뒤 선택하세요.',parent=self.root);return
        if uid in self.chosen:
            self.chosen.difference_update(r['id'] for r in pair)
        else:
            self.chosen.update(r['id'] for r in pair)
        self.render_preview()

    def edit_import_row(self):
        uid=self.import_table.selected()
        if not uid or not self.preview:
            return
        collection,key=uid.split('/',1);rows=getattr(self.preview,collection);index=next(i for i,r in enumerate(rows) if r['id']==key)
        opened_preview=self.preview
        def done(item):
            if self.preview is not opened_preview:
                raise ValueError('검토 자료가 바뀌었습니다. 최신 자료를 다시 여세요.')
            item.update(_blocked=False,_warnings=['사용자가 값을 검토했습니다.']);rows[index]=item
            if collection=='trades':
                pair=next((g for g in groups(self.preview) if any(r['id']==item['id'] for r in g)),[item])
                self.chosen.difference_update(r['id'] for r in pair)
                if not any(r.get('_blocked') for r in pair):
                    self.chosen.update(r['id'] for r in pair)
            self.render_preview()
        self.edit(collection,rows[index],preview_callback=done,preview_state=self.preview_state())

    def exclude_import_event(self):
        uid=self.import_table.selected()
        if uid and not uid.startswith('trades/'):
            key,identifier=uid.split('/',1);row=next(r for r in getattr(self.preview,key) if r['id']==identifier)
            row.update(_blocked=True,_warnings=['사용자가 제외했습니다. 다시 포함하려면 수정하세요.']);self.render_preview()

    def commit_import(self):
        self.idle()
        if not self.preview:
            return
        expected=copy.deepcopy(self.state);target=account_choices(self.state).get(self.import_account.get())
        result,stats=stage_import(expected,self.preview,self.chosen,target,self.include_events.get(),self.update_orders.get())
        if messagebox.askyesno('검토 기록 저장',f"거래/수령 추가 {stats['added']}건 · 갱신 {stats['updated']}건\n참고 일정 {stats['events']}건 · 제외/중복 {stats['skipped']}건\n\n이 내용으로 저장할까요?",parent=self.root):
            self.save(result,expected);self.render_preview()

    def save_settings(self):
        self.idle();values={k:v.get().strip() for k,v in self.setting_vars.items()};interval=int(values['quote_interval'])
        if not 30<=interval<=86400:
            raise ValueError('시세 갱신 간격은 30~86400초로 입력하세요.')
        if self.auto_sync.get() and (not values['sync_repo'] or not values['github_token']):
            raise ValueError('자동 동기화에는 비공개 저장소와 토큰이 필요합니다.')
        if values['sync_repo']:
            from .sync import GitHubSync
            GitHubSync(values['sync_repo'],values['github_token'] or 'not-configured',values['sync_branch'] or 'main')
        for key in ('nh_app_key','nh_app_secret','github_token'):
            self.secrets.set(key,values.pop(key))
        self.settings.update(values,quote_interval=interval,auto_quotes=self.auto_quotes.get(),auto_sync=self.auto_sync.get());write_json(self.directory/'settings.json',self.settings)
        self.status.set('설정을 저장했습니다. 인증값은 이 PC에만 보관합니다.' if self.secrets.supported else '이 운영체제에서는 인증값을 메모리에만 보관합니다.')

    def provider(self):
        from .providers import NHProvider
        key=self.secrets.get('nh_app_key');secret=self.secrets.get('nh_app_secret')
        if not key or not secret:
            raise ValueError('설정에서 NH 앱키·시크릿을 먼저 저장하세요.')
        return NHProvider(key,secret,self.directory/'nh-cache')

    def load_master(self):
        from .providers import load_nh_master
        def done(rows):
            state=copy.deepcopy(self.state)
            for row in rows:
                previous=state['instruments'].get(row['id'])
                if previous and previous.get('deleted'):
                    continue
                state['instruments'][row['id']]=record(**({**row,**previous,'name':row['name']} if previous else row))
            self.save(state);self.status.set(f'공식 종목 목록 {len(rows):,}개를 확인했습니다.')
        self.work('NH 공개 종목 목록을 내려받습니다…',load_nh_master,done)

    def refresh_quotes(self,quiet=False):
        if self.busy:
            return
        try:
            ids={r['instrument_id'] for r in portfolio(self.state)['rows'] if r['quantity']>0}
            instruments=[self.state['instruments'][uid] for uid in ids if self.state['instruments'][uid]['market']=='KR']
            if not instruments:
                self.status.set('조회할 국내 보유 종목이 없습니다.');return
            provider=self.provider()
        except Exception as exc:
            if not quiet:
                self.safe(lambda:(_ for _ in ()).throw(exc))
            return
        def done(quotes):
            self.quotes.update(quotes);write_json(self.directory/'quotes.json',self.quotes);self.last_quote=time.time();self.refresh();self.status.set(f'NH 현재가 {len(quotes)}종목 갱신 · 주기 조회')
        self.work('국내 보유 종목 현재가를 조회합니다…',lambda:provider.quotes(instruments),done)

    def tick(self):
        if not self.smoke and self.settings.get('auto_quotes') and time.time()-self.last_quote>max(30,int(self.settings.get('quote_interval',60))):
            self.last_quote=time.time();self.refresh_quotes(quiet=True)
        self.root.after(15000,self.tick)

    def import_nh(self):
        choices=account_choices(self.state)
        if not choices:
            raise ValueError('먼저 계좌를 등록하세요.')
        def query(values):
            api=self.provider();self.secrets.set('nh_account_'+values['account_id'],values['number'])
            def done(rows):
                from .importers import ImportPreview
                instruments={}
                for row in rows:
                    row['account_id']=values['account_id'];uid=row['instrument_id']
                    if uid not in self.state['instruments']:
                        instruments[uid]=record(id=uid,market='KR',code=uid.split(':',1)[1],name=row.get('_instrument_name') or uid,currency='KRW',asset_type='주식',sector='')
                self.load_preview(ImportPreview(instruments=list(instruments.values()),trades=rows,issues=['기간 이전 보유분은 먼저 기초 보유나 과거 거래로 등록하세요.','주문별 누적 체결입니다. 반복 조회한 주문을 갱신하려면 검토 후 갱신 허용을 선택하세요.']))
            self.work('NH 체결을 조회합니다…',lambda:api.trades(values['number'],values['start'],values['end']),done)
        selected=self.account_id() or next(iter(choices.values()))
        FormDialog(self.root,'NH 체결 가져오기',[('account_id','저장 계좌',selected,choices),('number','계좌번호',self.secrets.get('nh_account_'+selected),None),('start','시작일',today()[:8]+'01',None),('end','종료일',today(),None)],query,'조회만 수행합니다. 수수료·세금·동일 날짜 거래 순서는 증권사 원장과 비교하세요.')

    def sync(self,resolutions=None):
        self.pending_sync=None
        if self.busy:
            if self.settings.get('auto_sync'):
                self.pending_sync=self.root.after(3000,self.sync)
            return
        from .sync import GitHubSync,SyncConflict
        try:
            repo=self.settings.get('sync_repo','');branch=self.settings.get('sync_branch','main');identity=repo+'@'+branch
            api=GitHubSync(repo,self.secrets.get('github_token'),branch)
            info=read_json(self.directory/'sync-base.json',{});base=info.get('state') if info.get('identity')==identity else None
            local=copy.deepcopy(self.state);self.backup()
        except Exception as exc:
            messagebox.showinfo('동기화 설정',str(exc),parent=self.root);self.tabs.select(self.pages['settings']);return
        def done(result):
            self.save(result['state'],expected=local,autosync=False)
            write_json(self.directory/'sync-base.json',{'identity':identity,'state':result['state'],'sha':result['sha']})
            self.settings.update(dirty=False,last_sync=now());write_json(self.directory/'settings.json',self.settings);self.status.set('비공개 기록 저장소와 동기화했습니다. · '+datetime.now().strftime('%H:%M:%S'))
        def failed(error):
            if not isinstance(error,SyncConflict):
                raise error
            self.resolve_conflicts(error)
        self.work('비공개 기록 저장소와 동기화합니다…',lambda:api.synchronize(local,base,resolutions),done,failed)

    def resolve_conflicts(self,error):
        dialog=tk.Toplevel(self.root);dialog.title('동시에 수정된 기록 확인');dialog.geometry('1000x620');dialog.transient(self.root);dialog.grab_set()
        label=ttk.Label(dialog,padding=15);label.pack(fill='x');body=ttk.Frame(dialog);body.pack(fill='both',expand=True);boxes=[]
        for name in ('이 PC','GitHub'):
            frame=ttk.LabelFrame(body,text=name,padding=8);frame.pack(side='left',fill='both',expand=True,padx=8)
            box=tk.Text(frame,width=45,wrap='word',font=('Malgun Gothic',10));box.pack(fill='both',expand=True);boxes.append(box)
        index=[0];choices={}
        def render():
            item=error.conflicts[index[0]];label.configure(text=f"{index[0]+1}/{len(error.conflicts)} · {item['path']}")
            for box,key in zip(boxes,('local','remote')):
                box.configure(state='normal');box.delete('1.0','end');box.insert('1.0',json.dumps(item[key],ensure_ascii=False,indent=2));box.configure(state='disabled')
        def choose(side):
            choices[error.conflicts[index[0]]['path']]=side;index[0]+=1
            if index[0]==len(error.conflicts):
                dialog.destroy();self.sync({'choices':choices,'remote_sha':error.remote_sha,'local_fingerprint':error.local_fingerprint})
            else:
                render()
        bar=ttk.Frame(dialog,padding=12);bar.pack(fill='x');ttk.Button(bar,text='취소',command=dialog.destroy).pack(side='right')
        ttk.Button(bar,text='GitHub 값 사용',command=lambda:choose('remote')).pack(side='right',padx=8);ttk.Button(bar,text='이 PC 값 사용',command=lambda:choose('local')).pack(side='right');render()

    def export_backup(self):
        path=filedialog.asksaveasfilename(parent=self.root,defaultextension='.json',initialfile='stock-journal-'+today()+'.json')
        if path:
            write_json(Path(path),self.state);self.status.set('기록 백업을 저장했습니다. 인증값은 포함하지 않습니다.')

    def restore_backup(self):
        self.idle();expected=copy.deepcopy(self.state)
        path=filedialog.askopenfilename(parent=self.root,filetypes=[('JSON 기록 백업','*.json')])
        if not path:
            return
        state=json.loads(Path(path).read_text(encoding='utf-8'));validate_state(state)
        if messagebox.askyesno('백업 복원','현재 기록을 백업한 뒤 선택한 원장으로 교체할까요?',parent=self.root):
            self.save(state,expected=expected,autosync=False)

    def export_csv(self):
        path=filedialog.asksaveasfilename(parent=self.root,defaultextension='.csv',initialfile='trades-'+today()+'.csv')
        if not path:
            return
        fields=['executed_at','account','market','code','name','currency','side','quantity','price','fee','tax','external_id','memo']
        with open(path,'w',encoding='utf-8-sig',newline='') as handle:
            writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader()
            for row in self.active('trades'):
                if self.account_id() and row['account_id']!=self.account_id():
                    continue
                instrument=self.state['instruments'][row['instrument_id']]
                out={**row,**{k:instrument[k] for k in ('market','code','name','currency')},'account':self.state['accounts'][row['account_id']]['name']}
                writer.writerow({k:out.get(k,'') for k in fields})
        self.status.set('조회용 CSV를 내보냈습니다. 원장 전체 백업·복원은 JSON을 사용하세요.')

    def close(self):
        if self.busy:
            messagebox.showinfo('작업 진행 중','조회·동기화가 끝난 뒤 종료해 주세요.',parent=self.root);return
        if not self.smoke:
            self.backup()
        self.store.close();self.lock.close();self.root.destroy()


def main(argv=None):
    parser=argparse.ArgumentParser(description='Windows 개인 주식 매매일지')
    parser.add_argument('--data-dir',type=Path);parser.add_argument('--smoke-test',action='store_true');parser.add_argument('--smoke-report',type=Path);parser.add_argument('--screenshot',type=Path)
    args=parser.parse_args(argv)
    if args.smoke_test:
        from .smoke import run
        return run(args.smoke_report,args.screenshot)
    root=None
    try:
        root=tk.Tk();JournalApp(root,args.data_dir);root.mainloop();return 0
    except Exception as exc:
        import traceback
        directory=args.data_dir or data_directory();directory.mkdir(parents=True,exist_ok=True);(directory/'startup-error.log').write_text(traceback.format_exc(),encoding='utf-8')
        if root:
            messagebox.showerror('실행 오류',str(exc)+'\n\n오류 기록: '+str(directory/'startup-error.log'),parent=root)
        return 1
