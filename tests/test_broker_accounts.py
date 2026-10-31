import copy
import unittest

from stockjournal.domain import empty_state, portfolio
from stockjournal.importers import ImportPreview
from stockjournal.workflow import record, stage_import


def base():
    state = empty_state()
    state['accounts']['nh'] = record(id='nh', name='NH 테스트', broker='NH투자증권', kind='일반', currency='KRW', connection_mode='nh')
    state['accounts']['manual'] = record(id='manual', name='다른 증권', broker='삼성증권', kind='DC', currency='KRW')
    return state


def snapshot():
    return {'as_of':'2026-10-30T23:00:00+00:00', 'source':'nh:test-balance', 'holdings':[
        {'instrument_id':'US:TQQQ', 'market':'US', 'code':'TQQQ', 'name':'TQQQ', 'currency':'USD',
         'quantity':'10', 'average_price':'50', 'book_value':'500', 'current_price':'60',
         'position_type':'', 'cash_position':False, 'loan_date':'', 'loan_balance':'0'}]}


def trade(uid='fill', **overrides):
    return record(id=uid,account_id='nh',instrument_id='US:TQQQ',currency='USD',executed_at='2026-10-30',
                  side='BUY',quantity='10',price='50',fee='0',tax='0',source='NH',external_id=uid,
                  **overrides)


class BrokerAccountsTests(unittest.TestCase):
    def test_bound_nh_opening_cannot_be_redirected_to_a_stale_dropdown_account(self):
        from stockjournal.broker_accounts import opening_preview
        p=opening_preview(base(),'nh',snapshot(),{'US:TQQQ'},cash_confirmed=True)
        with self.assertRaises(ValueError):
            stage_import(base(),p,account_override='manual')

    def test_distinct_real_nh_orders_on_same_day_are_not_manual_duplicates(self):
        from stockjournal.broker_accounts import broker_import_preview
        state=base();state['instruments']['US:TQQQ']=record(id='US:TQQQ',market='US',code='TQQQ',name='TQQQ',currency='USD',asset_type='ETF',sector='')
        state['trades']['first']=trade('first')
        self.assertFalse(broker_import_preview(state,'nh',[trade('second')]).trades[0]['_blocked'])

    def test_binding_existing_nh_keeps_history_and_never_stores_number(self):
        from stockjournal.broker_accounts import bind_account
        state=base(); old=copy.deepcopy(state)
        account={'account_number':'12345678901','account_type':'01','environment':'live','usable':True}
        result,uid=bind_account(state,account,existing_id='nh')
        self.assertEqual(uid,'nh');self.assertEqual(state,old)
        self.assertNotIn('12345678901',str(result))
        self.assertTrue(result['accounts'][uid]['broker_fingerprint'])
        again,other=bind_account(result,account)
        self.assertEqual(other,'nh');self.assertEqual(len(again['accounts']),2)

    def test_binding_other_broker_or_mock_is_rejected(self):
        from stockjournal.broker_accounts import bind_account
        account={'account_number':'12345678901','account_type':'01','environment':'live','usable':True}
        with self.assertRaises(ValueError):bind_account(base(),account,existing_id='manual')
        with self.assertRaises(ValueError):bind_account(base(),dict(account,environment='mock',usable=False))

    def test_opening_needs_cash_confirmation_and_keeps_precise_cost(self):
        from stockjournal.broker_accounts import opening_preview
        with self.assertRaises(ValueError):opening_preview(base(),'nh',snapshot(),{'US:TQQQ'})
        p=opening_preview(base(),'nh',snapshot(),{'US:TQQQ'},cash_confirmed=True)
        state,_=stage_import(base(),p)
        self.assertEqual(portfolio(state,account_id='nh')['rows'][0]['cost'],500)
        self.assertEqual(p.trades[0]['side'],'OPENING')

    def test_snapshot_is_not_added_on_top_of_existing_history(self):
        from stockjournal.broker_accounts import opening_preview, holdings_comparison
        p=opening_preview(base(),'nh',snapshot(),{'US:TQQQ'},cash_confirmed=True)
        state,_=stage_import(base(),p)
        self.assertEqual(holdings_comparison(state,'nh',snapshot())[0]['difference'],0)
        with self.assertRaises(ValueError):opening_preview(state,'nh',snapshot(),{'US:TQQQ'},cash_confirmed=True)

    def test_missing_broker_position_is_shown_for_existing_holding(self):
        from stockjournal.broker_accounts import opening_preview, holdings_comparison
        p=opening_preview(base(),'nh',snapshot(),{'US:TQQQ'},cash_confirmed=True)
        state,_=stage_import(base(),p)
        rows=holdings_comparison(state,'nh',dict(snapshot(),holdings=[],market='US'))
        self.assertEqual(rows[0]['broker_quantity'],0)
        self.assertEqual(rows[0]['difference'],-10)

    def test_api_import_same_day_legacy_is_blocked_for_review(self):
        from stockjournal.broker_accounts import broker_import_preview
        state=base();state['instruments']['US:TQQQ']=record(id='US:TQQQ',market='US',code='TQQQ',name='TQQQ',currency='USD',asset_type='ETF',sector='')
        existing=trade('old');existing['source']='xlsx:매매일지';state['trades']['old']=existing
        p=broker_import_preview(state,'nh',[trade()])
        self.assertTrue(p.trades[0]['_blocked'])
        self.assertTrue(any('중복' in w for w in p.trades[0]['_warnings']))

    def test_api_before_opening_is_blocked_and_same_provider_update_is_allowed(self):
        from stockjournal.broker_accounts import opening_preview, broker_import_preview
        state,_=stage_import(base(),opening_preview(base(),'nh',snapshot(),{'US:TQQQ'},cash_confirmed=True))
        self.assertTrue(broker_import_preview(state,'nh',[trade()]).trades[0]['_blocked'])
        state=base();state['instruments']['US:TQQQ']=record(id='US:TQQQ',market='US',code='TQQQ',name='TQQQ',currency='USD',asset_type='ETF',sector='')
        state['trades']['fill']=trade()
        self.assertFalse(broker_import_preview(state,'nh',[trade()]).trades[0]['_blocked'])

    def test_manual_account_cannot_receive_api_records(self):
        from stockjournal.broker_accounts import broker_import_preview
        with self.assertRaises(ValueError):broker_import_preview(base(),'manual',[trade()])

