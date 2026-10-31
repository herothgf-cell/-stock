import copy
import unittest

from stockjournal.domain import empty_state
from stockjournal.importers import ImportPreview
from stockjournal.workflow import record


def preview():
    account=record(id='a',name='NH증권_IRP',broker='',kind='brokerage',currency='KRW')
    instrument=record(id='KR:005930',market='KR',code='005930',name='테스트',currency='KRW',asset_type='주식',sector='')
    buy=record(id='buy',account_id='a',instrument_id=instrument['id'],currency='KRW',executed_at='2026-01-01',side='BUY',quantity='2',price='100',fee='0',tax='0',source='xlsx:매매일지',external_id='row2',_warnings=['원본 수수료 확인'])
    pending=dict(buy,id='pending',external_id='row3',_blocked=True,_warnings=['날짜 확인'])
    return ImportPreview(accounts=[account],instruments=[instrument],trades=[buy,pending])


class InitializationTests(unittest.TestCase):
    def test_existing_alias_mapping_remains_idempotent_and_pending_uses_same_account(self):
        from stockjournal.initialization import make_package, merge_package
        package=make_package(preview(),{'title':'합성'})
        state=empty_state()
        state['accounts']['existing']={**package['state']['accounts']['a'],'id':'existing'}
        first,pending,_=merge_package(state,package)
        second,pending2,_=merge_package(first,package)
        self.assertEqual(first,second)
        self.assertEqual(first['trades']['buy']['account_id'],'existing')
        self.assertEqual(pending.trades[0]['account_id'],'existing')
        self.assertEqual(pending,pending2)

    def test_reimport_keeps_reviewed_and_unrelated_pending_rows(self):
        from stockjournal.initialization import make_package, read_package, merge_pending
        package=make_package(preview(),{'title':'합성'});state,incoming=read_package(package)
        existing=copy.deepcopy(incoming);existing.trades[0]['price']='123'
        extra=copy.deepcopy(existing.trades[0]);extra['id']='other-pending';existing.trades.append(extra)
        combined=merge_pending(existing,incoming,state)
        rows={row['id']:row for row in combined.trades}
        self.assertEqual(rows['pending']['price'],'123');self.assertIn('other-pending',rows)
        saved=copy.deepcopy(state);saved['trades']['pending']={**state['trades']['buy'],'id':'pending'}
        self.assertNotIn('pending',{row['id'] for row in merge_pending(existing,incoming,saved).trades})

    def test_excel_raw_dates_can_be_saved_in_private_package(self):
        from stockjournal.initialization import make_package
        from datetime import datetime
        import json
        p=preview();p.trades[1]['_raw']={'date':datetime(2026,1,1)}
        package=make_package(p,{'title':'합성'})
        self.assertIn('2026-01-01',json.dumps(package,ensure_ascii=False))

    def test_valid_initial_data_and_pending_are_preserved_separately(self):
        from stockjournal.initialization import make_package, read_package
        p=make_package(preview(),{'title':'합성 입력','retrieved_at':'2026-10-31T00:00:00Z'})
        state,pending=read_package(p)
        self.assertEqual(set(state['trades']),{'buy'})
        self.assertEqual([r['id'] for r in pending.trades],['pending'])
        self.assertEqual(state['accounts']['a']['kind'],'IRP')
        self.assertEqual(state['accounts']['a']['connection_mode'],'nh')
        self.assertEqual(state['trades']['buy']['import_notes'],['원본 수수료 확인'])

    def test_initial_import_is_idempotent_and_keeps_unrelated_records(self):
        from stockjournal.initialization import make_package, merge_package
        p=make_package(preview(),{'title':'합성'})
        current=empty_state();current['accounts']['other']=record(id='other',name='다른 계좌',broker='삼성증권',kind='DC',currency='KRW')
        one,_,_=merge_package(current,p);two,_,_=merge_package(one,p)
        self.assertEqual(one,two);self.assertIn('other',two['accounts'])
        self.assertEqual(current['trades'],{})

    def test_same_id_changed_trade_does_not_silently_overwrite(self):
        from stockjournal.initialization import make_package, merge_package
        p=make_package(preview(),{'title':'합성'});state,_,_=merge_package(empty_state(),p)
        state['trades']['buy']['price']='101'
        before=copy.deepcopy(state)
        with self.assertRaises(ValueError):merge_package(state,p)
        self.assertEqual(before,state)

    def test_unexpected_package_fields_and_non_list_pending_fail(self):
        from stockjournal.initialization import make_package, read_package
        p=make_package(preview(),{'title':'합성'})
        p['pending']['trades']='bad'
        with self.assertRaises(ValueError):read_package(p)

    def test_manual_brokers_and_account_kinds_are_retained(self):
        from stockjournal.initialization import make_package
        p=preview();p.accounts[0].update(name='삼성증권_DC')
        package=make_package(p,{'title':'합성'})
        a=package['state']['accounts']['a']
        self.assertEqual(a['connection_mode'],'manual');self.assertEqual(a['kind'],'DC');self.assertEqual(a['broker'],'삼성증권')

