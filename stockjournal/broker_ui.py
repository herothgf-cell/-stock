"""Native dialogs for connecting NH accounts and reviewing broker data."""
import copy
import tkinter as tk
from tkinter import ttk, messagebox

from .broker_accounts import bind_account, is_nh_account, holdings_comparison, opening_preview, broker_import_preview
from .ui import Table, FormDialog, money
from .workflow import account_choices


class BrokerController:
    def __init__(self, app):
        self.app=app

    def choices(self):
        state=self.app.state
        return {label:uid for label,uid in account_choices(state).items() if is_nh_account(state['accounts'][uid])}

    def number(self, account_id):
        value=self.app.secrets.get('nh_account_'+account_id)
        if not value:
            raise ValueError('이 PC에서 나무 계좌를 먼저 연결하세요. 설정 → 나무 계좌 가져오기에서 기존 별칭을 선택할 수 있습니다.')
        return value

    def discover(self):
        app=self.app;api=app.provider()
        def done(rows):
            dialog=tk.Toplevel(app.root);dialog.title('나무 계좌 가져오기');dialog.geometry('830x500');dialog.transient(app.root)
            ttk.Label(dialog,text='계좌를 선택해 기존 별칭에 연결하거나 새 계좌로 추가하세요. 계좌번호는 이 PC에 암호화하여 저장합니다.',wraplength=790,padding=14).pack(fill='x')
            table=Table(dialog,[('number','계좌번호',250),('type','API 계좌 구분',230),('state','연결 가능',200)],height=7);table.pack(fill='both',expand=True,padx=14)
            for index,row in enumerate(rows):
                table.tree.insert('','end',iid=str(index),values=(row['masked_number'],{'01':'운영 · 일반','02':'운영 · 주문대리인','03':'모의투자'}.get(row['account_type'],'미지원'), '가능' if row['usable'] and row['environment']=='live' else '매매일지 연결 제외'))
            def connect():
                uid=table.selected()
                if uid is None:return
                item=rows[int(uid)]
                if not item['usable'] or item['environment']!='live':
                    raise ValueError('운영 계좌를 선택하세요. 모의투자 계좌는 실제 매매일지에 연결하지 않습니다.')
                choices={'새 계좌로 추가':'',**self.choices()}
                expected=copy.deepcopy(app.state)
                def commit(values):
                    state,account_id=bind_account(app.state,item,existing_id=values['existing'] or None,name=values['name'],kind=values['kind'])
                    app.save(state,expected=expected)
                    app.secrets.set('nh_account_'+account_id,item['account_number'])
                    app.settings.setdefault('nh_account_types',{})[account_id]=item['account_type']
                    from .app import write_json
                    write_json(app.directory/'settings.json',app.settings)
                    app.refresh();app.status.set('나무 계좌를 연결했습니다. 잔고 대조 또는 체결 조회를 사용할 수 있습니다.')
                FormDialog(dialog,'계좌 연결',[('existing','연결할 기존 계좌','',choices),('name','새 계좌 별칭','나무 '+item['masked_number'][-4:],None),('kind','새 계좌 종류','일반',{v:v for v in ('일반','ISA','연금저축','IRP','DC','기타')})],commit,'기존 계좌를 선택하면 별칭·기록을 유지합니다. 새 계좌 별칭·종류는 새로 추가할 때만 사용합니다.')
            bar=ttk.Frame(dialog,padding=14);bar.pack(fill='x');ttk.Button(bar,text='선택 계좌 연결',command=lambda:app.safe(connect),style='Primary.TButton').pack(side='left');ttk.Button(bar,text='닫기',command=dialog.destroy).pack(side='right')
            app.status.set(f'나무 연결 계좌 {len(rows)}개를 조회했습니다.')
        app.work('나무 계좌 목록을 조회합니다…',api.discover_accounts,done)

    def _query_form(self, title, query, *, dates=False):
        app=self.app;choices=self.choices()
        if not choices:
            raise ValueError('설정에서 나무 계좌를 가져오거나 나무 계좌 별칭을 먼저 등록하세요.')
        selected=app.account_id() if app.account_id() in choices.values() else next(iter(choices.values()))
        fields=[('account_id','나무 계좌',selected,choices),('market','조회 시장','KR',{'국내':'KR','미국':'US'})]
        if dates:
            from .app import today
            fields += [('start','시작일',today()[:8]+'01',None),('end','종료일',today(),None)]
        def begin(values):
            number=self.number(values['account_id'])
            query(values,number)
        FormDialog(app.root,title,fields,begin,'조회 자료는 먼저 검토합니다. 다른 증권사 계좌는 매매 입력 또는 엑셀·CSV로 관리합니다.')

    def import_trades(self):
        app=self.app
        def query(values,number):
            api=app.provider()
            request=api.overseas_trades if values['market']=='US' else api.trades
            def done(rows):
                app.load_preview(broker_import_preview(app.state,values['account_id'],rows))
                label=next(name for name,uid in account_choices(app.state).items() if uid==values['account_id'])
                app.import_account.set(label)
            app.work('나무 체결 내역을 조회합니다…',lambda:request(number,values['start'],values['end']),done)
        self._query_form('나무 체결 가져오기',query,dates=True)

    def holdings(self):
        app=self.app
        def query(values,number):
            api=app.provider()
            def done(snapshot):
                snapshot['market']=values['market']
                self.show_holdings(values['account_id'],snapshot)
            app.work('나무 잔고와 매매일지 보유량을 대조합니다…',lambda:api.get_holdings(number,overseas=values['market']=='US'),done)
        self._query_form('나무 잔고 대조',query)

    def show_holdings(self, account_id, snapshot):
        app=self.app;comparison=holdings_comparison(app.state,account_id,snapshot)
        dialog=tk.Toplevel(app.root);dialog.title('나무 잔고 · 기록 대조');dialog.geometry('970x600');dialog.transient(app.root)
        ttk.Label(dialog,text=f"{app.state['accounts'][account_id]['name']} · {snapshot['as_of']}\n수량 차이를 확인하세요. 기존 거래가 있는 종목에는 잔고를 추가하지 않습니다.",padding=14).pack(fill='x')
        bar=ttk.Frame(dialog,padding=12);bar.pack(side='bottom',fill='x')
        cash=tk.BooleanVar(value=False)
        ttk.Checkbutton(bar,text='선택 종목이 신용·대출이 없는 현금 보유임을 확인했습니다.',variable=cash).pack(anchor='w',pady=(0,8))
        buttons=ttk.Frame(bar);buttons.pack(fill='x')
        table=Table(dialog,[('name','종목',210),('code','코드',100),('broker','나무 잔고',130),('journal','기록 보유량',130),('delta','차이',110),('opening','기초 보유 입력',180)],height=8);table.pack(fill='both',expand=True,padx=14)
        table.tree.configure(selectmode='extended')
        for row in comparison:
            table.tree.insert('','end',iid=row['instrument_id'],values=(row['name'],row['instrument_id'],money(row['broker_quantity'],6),money(row['journal_quantity'],6),money(row['difference'],6),'가능 · 과거 이력 없음' if row['opening_allowed'] else '대조만 가능'))
        notes='\n'.join(snapshot.get('warnings',[]))
        ttk.Label(dialog,text=notes,wraplength=920,foreground='#7a4a10',padding=12).pack(fill='x')
        def prepare():
            selected=set(table.tree.selection())
            if not selected:raise ValueError('기초 보유로 가져올 종목을 선택하세요.')
            preview=opening_preview(app.state,account_id,snapshot,selected,cash_confirmed=cash.get())
            app.load_preview(preview);dialog.destroy()
        ttk.Button(buttons,text='선택 종목 기초 보유 검토',command=lambda:app.safe(prepare),style='Primary.TButton').pack(side='left')
        ttk.Button(buttons,text='닫기',command=dialog.destroy).pack(side='right')
        app.status.set(f'잔고 대조 {len(comparison)}종목 · 실제 원장은 변경하지 않았습니다.')
