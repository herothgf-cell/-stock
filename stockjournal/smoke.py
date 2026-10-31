"""Source and frozen EXE checks use an isolated synthetic journal only."""
import copy
from decimal import Decimal
import importlib
import json
from pathlib import Path
import sys
import tempfile
import traceback


def fixture():
    from .domain import empty_state
    from .workflow import record
    state=empty_state()
    for uid,currency in [('a','KRW'),('b','USD'),('c','EUR')]:
        state['accounts'][uid]=record(id=uid,name='테스트 '+currency,broker='합성 자료',kind='일반',currency=currency)
    for uid,currency,name in [('KR:0000A0','KRW','테스트 보유 ETF'),('US:SMOKE','USD','테스트 매도 완료'),('EU:SMOKE','EUR','테스트 미조회')]:
        market,code=uid.split(':');state['instruments'][uid]=record(id=uid,market=market,code=code,name=name,currency=currency,asset_type='주식',sector='테스트')
    rows=[('buy1','a','KR:0000A0','BUY','10','100','10','0',1),('sell1','a','KR:0000A0','SELL','2','120','2','1',2),('buy2','b','US:SMOKE','BUY','2','10','1','0',1),('sell2','b','US:SMOKE','SELL','2','15','1','0',2),('buy3','c','EU:SMOKE','BUY','2','4','0','0',1)]
    for uid,account,instrument,side,qty,price,fee,tax,sequence in rows:
        state['trades'][uid]=record(id=uid,account_id=account,instrument_id=instrument,executed_at='2026-01-01',side=side,quantity=qty,price=price,fee=fee,tax=tax,sequence=sequence,currency=state['instruments'][instrument]['currency'],memo='합성 테스트',source='smoke')
    for uid,account,instrument,gross,tax,currency in [('d1','a','KR:0000A0','10','1','KRW'),('d2','b','US:SMOKE','2','0','USD')]:
        state['dividends'][uid]=record(id=uid,account_id=account,instrument_id=instrument,paid_date='2026-01-02',gross=gross,tax=tax,currency=currency)
    state['dividend_events']['e1']=record(id='e1',instrument_id='KR:0000A0',record_date='2026-02-01',pay_date='2026-02-10',amount_per_share='1',currency='KRW',status='reference',source='합성 참고 일정')
    return state


def check(condition,message):
    if not condition:
        raise AssertionError(message)


def run(report_path=None,screenshot=None):
    report={'ok':False,'frozen':bool(getattr(sys,'frozen',False)),'pages':[],'assertions':[],'screenshots':[]}
    root=app=None
    try:
        import tkinter as tk
        from .app import JournalApp
        from .store import JournalStore
        from .domain import portfolio
        from .importers import ImportPreview
        with tempfile.TemporaryDirectory(prefix='stockjournal-smoke-') as directory:
            state=fixture()
            with JournalStore(Path(directory)/'journal.sqlite3') as store:
                store.save(state)
            root=tk.Tk();errors=[];root.report_callback_exception=lambda kind,value,tb:errors.append(''.join(traceback.format_exception(kind,value,tb)))
            try:
                app=JournalApp(root,directory,smoke=True)
                root.geometry(f"{min(1380,root.winfo_screenwidth()-24)}x{min(860,root.winfo_screenheight()-90)}+10+10")
                app.quotes={'KR:0000A0':{'price':'130','as_of':'2026-01-03T12:00:00+09:00','source':'합성 시세'}};app.refresh()
                candidate=copy.deepcopy(state['trades']['buy1']);candidate.update(id='preview1',executed_at='2026-01-04',_blocked=False,_warnings=['수수료·세금 확인 예시'])
                blocked=dict(candidate,id='preview2',side='SELL',_blocked=True,_warnings=['날짜·금액 수정 필요 예시'])
                app.load_preview(ImportPreview(trades=[candidate,blocked],issues=['실제 계좌가 아닌 합성 테스트 자료입니다.']))
                for name in ('dashboard','trades','imports','dividends','instruments','strategy','settings'):
                    app.tabs.select(app.pages[name]);root.update_idletasks();root.update();check(app.pages[name].winfo_exists(),name)
                    report['pages'].append(name)
                    if name=='imports':
                        page=app.pages[name]
                        for button in app.import_buttons.winfo_children():
                            check(button.winfo_ismapped() and button.winfo_rooty()+button.winfo_height()<=page.winfo_rooty()+page.winfo_height(),'Import action clipped: '+str(button.cget('text')))
                            check(button.winfo_rootx()+button.winfo_width()<=page.winfo_rootx()+page.winfo_width(),'Import action clipped horizontally')
                    if name=='settings':
                        page=app.pages[name]
                        for index,button in enumerate(app.settings_buttons):
                            app.connection_tabs.select(index);root.update_idletasks();root.update()
                            check(button.winfo_ismapped() and button.winfo_rooty()+button.winfo_height()<=page.winfo_rooty()+page.winfo_height(),'Connection save action clipped')
                        app.connection_tabs.select(0);root.update_idletasks();root.update()
                    if name=='strategy':
                        check(app.automation_panel.mode.get()=='DRY_RUN','live mode must never persist across startup')
                        check(not app.automation_panel.cash_account_verified.get(),'cash account permission must start unconfirmed')
                        for index in (0,1,0):
                            app.strategy_tabs.select(index);root.update_idletasks();root.update()
                        from zoneinfo import ZoneInfo
                        check(ZoneInfo('America/New_York').key=='America/New_York','exchange timezone data missing')
                    if screenshot:
                        target=Path(screenshot);target=target.with_name(target.stem+'-'+name+'.png');target.parent.mkdir(parents=True,exist_ok=True)
                        grab=importlib.import_module('PIL.ImageGrab')
                        box=(root.winfo_rootx(),root.winfo_rooty(),root.winfo_rootx()+root.winfo_width(),root.winfo_rooty()+root.winfo_height())
                        grab.grab(bbox=box,all_screens=True).save(target);check(target.stat().st_size>1000,'screenshot blank file');report['screenshots'].append(str(target))
                        if name=='strategy':
                            app.strategy_tabs.select(1);root.update_idletasks();root.update()
                            button=app.automation_panel.start_button;page=app.pages[name]
                            check(button.winfo_ismapped() and button.winfo_rootx()+button.winfo_width()<=page.winfo_rootx()+page.winfo_width(),'Automation start action clipped')
                            target=target.with_name('window-automation.png')
                            grab.grab(bbox=box,all_screens=True).save(target);report['screenshots'].append(str(target))
                            app.strategy_tabs.select(0);root.update_idletasks();root.update()
                totals=portfolio(app.state,app.quotes)['totals']
                for key,value in {'cost':'808','market_value':'1040','realized_pnl':'35','unrealized_pnl':'232','dividends_net':'9','total_pnl':'276'}.items():
                    check(totals['KRW'][key]==Decimal(value),'KRW '+key)
                check(totals['USD']['total_pnl']==Decimal('10'),'sold-out history')
                check(totals['EUR']['market_value'] is None,'unpriced valuation')
                check(len(app.holdings.tree.get_children())==3,'holdings table')
                check(len(app.trades.tree.get_children())==5,'trades table')
                check(app.chosen=={'preview1'},'blocked import selected')
                app.account.set('테스트 KRW');app.refresh()
                check(len(app.holdings.tree.get_children())==1,'account filter')
                check(len(app.receipts.tree.get_children())==1,'dividend filter')
                check(app.store.load()==state,'UI changed stored journal')
                check((Path(__file__).parent/'resources'/'windows_ocr.ps1').is_file(),'OCR script missing')
                check(not errors,'\n'.join(errors))
                report['assertions']=['currency_separation','fees_and_cost_basis','sold_out_history','net_dividends','unpriced_valuation','account_filters','blocked_import','stored_state_unchanged','ocr_resource_present','visible_import_actions','visible_connection_actions','strategy_and_automation_pages','dry_run_default','cash_account_unconfirmed','exchange_timezone_data']
                app.close();app=None;root=None;report['closed']=True
            finally:
                if app:
                    app.store.close();app.lock.close()
                if root:
                    root.destroy()
        report['ok']=True
    except Exception:
        report['error']=traceback.format_exc()
    if report_path:
        report_path=Path(report_path);report_path.parent.mkdir(parents=True,exist_ok=True);report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    if sys.stdout:
        print(json.dumps(report,ensure_ascii=True))
    return 0 if report['ok'] else 1
