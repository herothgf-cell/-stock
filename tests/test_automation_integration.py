"""Actual broker rows, rather than proposed orders, advance strategy state."""
import copy
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal as D
import json
import hashlib
import unittest
from unittest.mock import Mock, patch

from stockjournal.domain import empty_state, portfolio
from stockjournal.strategy import Action, Decision, FIELD, adopt_cycle, update_preferences
from stockjournal.workflow import record
from stockjournal.automation_integration import apply_verified_fills, execution_dates, prepare_context


NOW = datetime(2026, 10, 30, 23, 0, tzinfo=timezone.utc)
FINGERPRINT = "a" * 64


def wire(value):
    return json.loads(json.dumps(asdict(value), default=str))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.state = empty_state()
        self.state['accounts']['a'] = record(id='a', name='합성 나무', broker='NH투자증권', kind='일반', currency='USD', connection_mode='nh')
        for code in ('TQQQ', 'SPYM', 'SGOV'):
            self.state['instruments']['US:' + code] = record(id='US:' + code, code=code, market='US', name=code, currency='USD', asset_type='ETF', sector='')
        self.state['trades']['initial'] = record(id='initial', account_id='a', instrument_id='US:TQQQ', executed_at='2026-10-01', side='BUY', quantity='100', price='30', fee='0', tax='0', currency='USD', sequence=1, source='synthetic')
        self.state = update_preferences(self.state, 'a', cash_usd='0', now=NOW)
        self.state = adopt_cycle(self.state, 'a', start_date='2026-10-01', entry_count=3, now=NOW)
        self.decision = Decision(account_id='a', status='profit', as_of='2026-10-29', history_hash='verified-history',
            evaluated_at=NOW.isoformat(), cycle=copy.deepcopy(self.state['accounts']['a'][FIELD]['cycle']),
            actions=[Action('profit10', 'SELL_PROFIT', 'TQQQ', D('10'), None, '합성 익절', D('10'))],
            existing_trade_ids=tuple(self.state['trades']))
        self.handles = {}
        self.workflow = {'id':'workflow1', 'account_id':'a', 'broker_account_id':FINGERPRINT,
                         'mode':'LIVE', 'status':'COMPLETE', 'applied':False, 'decision':wire(self.decision), 'orders':[]}
        self.rows = []
        self.add_order('sell', 'SELL_PROFIT', 'TQQQ', 'SELL', '10', '50', 'profit10', '101')
        self.add_order('rotate', 'ROTATE_SPYM', 'SPYM', 'BUY', '7', '70', 'profit10', '102')

    def decode(self, handle):
        return copy.deepcopy(self.handles[handle])

    def add_order(self, leg, kind, symbol, side, quantity, price, action_id, number):
        self.handles[leg] = {'account_id':FINGERPRINT, 'environment':'live', 'order_number':number,
            'submitted_at':'2026-10-30T19:00:00+00:00', 'symbol':symbol, 'side':side, 'quantity':int(quantity),
            'limit_price':price, 'session':'regular', 'candidate_dates':['2026-10-30','2026-10-31']}
        self.workflow['orders'].append({'leg_id':leg, 'action_id':action_id, 'kind':kind, 'symbol':symbol, 'side':side,
            'status':'FILLED', 'quantity':int(quantity), 'broker_order_id':leg,
            'update':{'filled_quantity':quantity,'average_price':price,'commission_usd':'0','tax_usd':'0','updated_at':NOW.isoformat()}})
        uid = f'nh-us:{FINGERPRINT[:24]}:2026-10-30:{number}'
        self.rows.append(record(id=uid, external_id=uid, account_id='', instrument_id='US:'+symbol,
            executed_at='2026-10-30', side=side, quantity=quantity, price=price, fee='0', tax='0', currency='USD',
            sequence=int(number), source='NH', _instrument_market='US', _instrument_currency='USD',
            _instrument_code=symbol, _instrument_name=symbol, _warnings=['합성 비용 확인']))

    def apply(self, state=None):
        return apply_verified_fills(state or self.state, self.workflow, self.rows, decode_handle=self.decode, now=NOW)

    def test_only_matching_actual_fills_advance_profit_and_rotation(self):
        result = self.apply()
        config = result['accounts']['a'][FIELD]
        self.assertEqual(config['cycle']['profit_levels'], ['10'])
        self.assertEqual(D(config['cycle']['pending_spym_usd']), D('10'))
        positions = {r['instrument_id']:r['quantity'] for r in portfolio(result)['rows']}
        self.assertEqual(positions['US:TQQQ'], D('90'))
        self.assertEqual(positions['US:SPYM'], D('7'))
        self.assertEqual(result, self.apply(result), 'crash after save must not apply twice')
        self.assertEqual(self.state['accounts']['a'][FIELD]['cycle']['profit_levels'], [])

    def test_missing_ambiguous_or_different_cumulative_fill_is_rejected(self):
        for mutation in ('missing', 'ambiguous', 'quantity', 'side', 'fingerprint', 'limit'):
            with self.subTest(mutation=mutation):
                before = copy.deepcopy(self.rows)
                handles = copy.deepcopy(self.handles)
                if mutation == 'missing': self.rows.pop()
                if mutation == 'ambiguous':
                    extra = copy.deepcopy(self.rows[0]); extra['id'] = extra['id'].replace('2026-10-30','2026-10-31'); self.rows.append(extra)
                if mutation == 'quantity': self.rows[0]['quantity'] = '9'
                if mutation == 'side': self.rows[0]['side'] = 'BUY'
                if mutation == 'fingerprint': self.handles['sell']['account_id'] = 'b'*64
                if mutation == 'limit': self.rows[0]['price'] = '49'
                with self.assertRaises(ValueError): self.apply()
                self.rows, self.handles = before, handles

    def test_dry_run_partial_and_unknown_orders_cannot_enter_real_journal(self):
        for field, value in (('mode','DRY_RUN'), ('status','BLOCKED')):
            original = self.workflow[field]; self.workflow[field] = value
            with self.assertRaises(ValueError): self.apply()
            self.workflow[field] = original
        self.workflow['orders'][0]['status'] = 'PARTIAL'
        with self.assertRaises(ValueError): self.apply()

    def test_unverified_costs_remain_visible_and_require_budget_review(self):
        self.workflow['orders'][0]['update']['commission_usd'] = None
        self.workflow['orders'][0]['update']['tax_usd'] = None
        result = self.apply()
        self.assertTrue(result['accounts']['a'][FIELD]['costs_review_required'])
        self.assertTrue(result['trades'][self.rows[0]['id']]['import_notes'])

    def test_same_day_manual_trade_blocks_automatic_double_import(self):
        self.state['trades']['manual-sell'] = record(id='manual-sell', account_id='a', instrument_id='US:TQQQ',
            executed_at='2026-10-30', side='SELL', quantity='10', price='50', fee='0', tax='0', currency='USD', sequence=2, source='manual')
        with self.assertRaises(ValueError): self.apply()

    def test_dates_include_both_exchange_and_broker_business_dates(self):
        self.assertEqual(execution_dates(self.workflow, decode_handle=self.decode), ['2026-10-30','2026-10-31'])

    def test_next_korean_business_date_is_verified_without_inventing_fill_time(self):
        for row in self.rows:
            row['id'] = row['id'].replace('2026-10-30', '2026-10-31')
            row['external_id'] = row['id']; row['executed_at'] = '2026-10-31'
        result = self.apply()
        self.assertEqual(result['trades'][self.rows[0]['id']]['executed_at'], '2026-10-31')
        self.assertEqual(result['accounts']['a'][FIELD]['cycle']['profit_levels'], ['10'])

    def test_full_exit_replenishes_reserve_only_with_explicitly_known_costs(self):
        self.workflow['orders'] = []; self.rows = []; self.handles = {}
        self.decision.actions = [Action('exit', 'EXIT', 'TQQQ', D('100'), None, '하향 이탈')]
        self.decision.status = 'exit'; self.workflow['decision'] = wire(self.decision)
        self.add_order('exit', 'EXIT', 'TQQQ', 'SELL', '100', '50', 'exit', '111')
        self.add_order('sgov', 'ROTATE_SGOV', 'SGOV', 'BUY', '49', '100', 'exit', '112')
        result = self.apply(); config = result['accounts']['a'][FIELD]
        self.assertIsNone(config['cycle']); self.assertEqual(D(config['cash_usd']), D('5000'))
        self.workflow['orders'][0]['update']['tax_usd'] = None
        result = self.apply(); config = result['accounts']['a'][FIELD]
        self.assertTrue(config['reserve_review_required']); self.assertEqual(D(config['cash_usd']), D('0'))

    def context_fixture(self, moment=NOW):
        from stockjournal.automation import BrokerSnapshot, Quote
        from stockjournal.strategy import confirm_history
        from tests.test_strategy import history
        number='12345678901'
        state=copy.deepcopy(self.state)
        state['accounts']['a']['broker_fingerprint']=hashlib.sha256(('nh-account:'+number).encode()).hexdigest()
        state=update_preferences(state,'a',cash_usd='500',now=moment)
        series=history(end='2026-10-30',adjustment='unknown')
        state=confirm_history(state,'a',series,now=NOW)
        provider=Mock();provider.discover_accounts.return_value=[{'account_number':number,'account_type':'01','environment':'live','usable':True}]
        broker=Mock();broker.account_id=hashlib.sha256(number.encode()).hexdigest()
        broker.snapshot.return_value=BrokerSnapshot(broker.account_id,moment,{'TQQQ':D('100'),'SPYM':D('0'),'SGOV':D('2')},D('1000'),(),True,moment,True)
        broker.quote.return_value=Quote('SGOV',D('100'),moment)
        return state,number,provider,series,broker

    def test_live_context_uses_postclose_window_and_only_allocated_cash_reserve(self):
        state,number,provider,series,broker=self.context_fixture()
        with patch('stockjournal.nh_execution.NHExecutionAdapter',return_value=broker) as adapter:
            context=prepare_context(state,'a',number,provider,series,mode='LIVE',cash_confirmed=True,now=NOW)
        self.assertTrue(context.evidence.adjustment_confirmed)
        self.assertEqual(context.evidence.exit_reserve_cash_usd,D('300'))
        self.assertEqual(adapter.call_args.kwargs['session'],'afterhours')
        broker.submit_limit.assert_not_called()

    def test_live_context_does_not_trade_previous_close_during_regular_session(self):
        state,number,provider,series,_=self.context_fixture()
        with patch('stockjournal.nh_execution.NHExecutionAdapter') as adapter, self.assertRaises(ValueError):
            prepare_context(state,'a',number,provider,series,mode='LIVE',cash_confirmed=True,
                            now=datetime(2026,10,30,15,tzinfo=timezone.utc))
        adapter.assert_not_called()

    def test_weekend_dry_run_does_not_need_live_account_acknowledgement(self):
        moment=datetime(2026,10,31,12,tzinfo=timezone.utc)
        state,number,provider,series,broker=self.context_fixture(moment)
        with patch('stockjournal.nh_execution.NHExecutionAdapter',return_value=broker) as adapter:
            context=prepare_context(state,'a',number,provider,series,now=moment)
        self.assertEqual(context.evidence.expected_session_date,'2026-10-30')
        self.assertFalse(adapter.call_args.kwargs['enabled'])
        self.assertFalse(adapter.call_args.kwargs['cash_account_verified'])
        broker.submit_limit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
