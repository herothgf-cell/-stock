"""Regression tests for importing independent, editable preview records."""

import csv
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def importers(self):
        from stockjournal import importers

        return importers

    def write_csv(self, rows, name="trades.csv"):
        target = self.root / name
        with target.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerows(rows)
        return target

    def legacy_workbook(self, *, closed=False, quote=75000, sell_date="2025-01-10"):
        book = Workbook()
        master = book.active
        master.title = "종목"
        master.append(["", "종류", "업종", "종목코드", "종목명"])
        master.append(["", "우선주", "금융", "00680K", "미래에셋증권2우B"])
        journal = book.create_sheet("매매일지")
        journal.append(["날짜", "종목코드", "", "", "", "계좌", "종목명", "수량", "매수가"])
        journal.append([datetime(2025, 1, 2), "00680K", None, None, None, "연금", "미래에셋증권2우B", 3, 70000])
        journal.cell(2, 11, quote)
        journal.cell(2, 19, "처리 완료" if closed else "보유")
        journal.cell(2, 20, sell_date if closed else None)
        journal.cell(2, 21, "원본 메모")
        dividends = book.create_sheet("배당정보")
        dividends.append(["기준일", "종목코드", "종목명", "주당배당금"])
        dividends.append(["2025-03-31", "00680K", "미래에셋증권2우B", 350])
        target = self.root / "legacy.xlsx"
        book.save(target)
        book.close()
        return target

    def test_legacy_closed_row_preserves_instrument_and_links_both_legs(self):
        preview = self.importers().preview_workbook(self.legacy_workbook(closed=True))
        self.assertEqual([row["side"] for row in preview.trades], ["BUY", "SELL"])
        buy, sell = preview.trades
        self.assertEqual(buy["instrument_id"], "KR:00680K")
        self.assertEqual(buy["quantity"], "3")
        self.assertEqual(buy["price"], "70000")
        self.assertEqual(sell["price"], "75000")
        self.assertEqual(buy["lot_id"], sell["lot_id"])
        self.assertEqual(sell["linked_buy_id"], buy["id"])
        self.assertTrue(sell["_warnings"])
        self.assertFalse(buy["_blocked"])
        self.assertFalse(sell["_blocked"])
        self.assertEqual(preview.instruments[0]["sector"], "금융")
        self.assertEqual(preview.instruments[0]["asset_type"], "우선주")
        self.assertEqual(preview.dividends, [])
        self.assertEqual(preview.dividend_events[0]["amount_per_share"], "350")
        self.assertEqual(preview.dividend_events[0]["record_date"], "2025-03-31")
        for row in preview.trades + preview.accounts + preview.instruments + preview.dividend_events:
            self.assertTrue(row["id"])
            datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
            self.assertIs(row["deleted"], False)

    def test_purchase_identity_does_not_change_when_legacy_row_closes(self):
        first = self.importers().preview_workbook(self.legacy_workbook(quote=71000))
        second = self.importers().preview_workbook(self.legacy_workbook(closed=True, quote=80000))
        self.assertEqual(first.trades[0]["id"], second.trades[0]["id"])
        self.assertEqual(first.trades[0]["lot_id"], second.trades[0]["lot_id"])

    def test_legacy_formula_exit_blocks_both_legs(self):
        preview = self.importers().preview_workbook(self.legacy_workbook(closed=True, quote="=70000+5000"))
        self.assertEqual(len(preview.trades), 2)
        self.assertTrue(all(row["_blocked"] for row in preview.trades))
        self.assertTrue(preview.issues)
        self.assertNotEqual(preview.trades[1]["price"], "0")

    def test_legacy_sell_before_buy_blocks_both_legs(self):
        preview = self.importers().preview_workbook(self.legacy_workbook(closed=True, sell_date="2024-12-31"))
        self.assertTrue(all(row["_blocked"] for row in preview.trades))
        self.assertTrue(any("매도" in issue for issue in preview.issues))

    def test_missing_required_formula_cache_is_not_zeroed(self):
        target = self.legacy_workbook(closed=True)
        from openpyxl import load_workbook

        book = load_workbook(target)
        book["매매일지"]["H2"] = "=1+2"
        book.save(target)
        book.close()
        preview = self.importers().preview_workbook(target)
        self.assertTrue(all(row["_blocked"] for row in preview.trades))
        self.assertEqual(preview.trades[0]["quantity"], "")
        self.assertTrue(preview.issues)

    def test_csv_preserves_identical_distinct_fills_and_reimports_stably(self):
        target = self.write_csv([
            ["account", "code", "date", "side", "quantity", "price"],
            ["키움", "005930", "2025-02-03", "BUY", "1.5", "70000.25"],
            ["키움", "005930", "2025-02-03", "BUY", "1.5", "70000.25"],
        ])
        first = self.importers().preview_csv(target)
        second = self.importers().preview_csv(target)
        self.assertEqual(len(first.trades), 2)
        self.assertNotEqual(first.trades[0]["id"], first.trades[1]["id"])
        self.assertEqual([row["id"] for row in first.trades], [row["id"] for row in second.trades])
        self.assertFalse(any(row["_blocked"] for row in first.trades))
        self.assertEqual(first.trades[0]["quantity"], "1.5")
        self.assertEqual(first.trades[0]["price"], "70000.25")

    def test_explicit_trade_ids_are_scoped_to_effective_account_and_day(self):
        target = self.write_csv([
            ["account", "code", "date", "side", "quantity", "price", "external_id"],
            ["A", "005930", "2025-02-03", "BUY", "1", "70000", "fill1"],
            ["B", "005930", "2025-02-03", "BUY", "1", "70000", "fill1"],
            ["A", "005930", "2025-02-04", "BUY", "1", "70000", "fill1"],
        ])
        preview = self.importers().preview_csv(target)
        self.assertEqual(len({row["id"] for row in preview.trades}), 3)
        self.assertFalse(any(row["_blocked"] for row in preview.trades))

    def test_duplicate_explicit_id_blocks_all_conflicting_rows(self):
        target = self.write_csv([
            ["account", "code", "date", "side", "quantity", "price", "external_id"],
            ["A", "005930", "2025-02-03", "BUY", "1", "70000", "fill1"],
            ["A", "005930", "2025-02-03", "BUY", "2", "71000", "fill1"],
        ])
        preview = self.importers().preview_csv(target)
        self.assertTrue(all(row["_blocked"] for row in preview.trades))
        self.assertTrue(preview.issues)

    def test_invalid_financial_values_remain_editable_and_blocked(self):
        target = self.write_csv([
            ["account", "code", "date", "side", "quantity", "price", "fee", "tax"],
            ["A", "005930", "2025-02-30", "???", "NaN", "???", "", "-1"],
        ])
        preview = self.importers().preview_csv(target)
        row = preview.trades[0]
        self.assertTrue(row["_blocked"])
        self.assertEqual(row["quantity"], "NaN")
        self.assertEqual(row["price"], "???")
        self.assertEqual(row["fee"], "")
        self.assertTrue(row["_raw"])
        self.assertGreaterEqual(len(preview.issues), 5)

    def test_dividend_reference_csv_never_becomes_actual_income(self):
        target = self.write_csv([
            ["code", "name", "record_date", "pay_date", "amount_per_share"],
            ["005930", "삼성전자", "2025-03-31", "2025-05-15", "361"],
        ], "events.csv")
        preview = self.importers().preview_csv(target)
        self.assertEqual(len(preview.dividend_events), 1)
        self.assertEqual(preview.dividends, [])
        self.assertEqual(preview.trades, [])
        self.assertFalse(preview.dividend_events[0]["_blocked"])

    def test_dividend_income_requires_account_date_and_finite_gross(self):
        target = self.write_csv([
            ["account", "code", "paid_date", "gross", "tax", "currency"],
            ["A", "005930", "2025-04-15", "3610", "550", "KRW"],
            ["", "005930", "invalid", "Infinity", "0", "KRW"],
        ], "income.csv")
        preview = self.importers().preview_csv(target)
        self.assertEqual(len(preview.dividends), 2)
        self.assertEqual(preview.dividends[0]["gross"], "3610")
        self.assertEqual(preview.dividends[0]["tax"], "550")
        self.assertFalse(preview.dividends[0]["_blocked"])
        self.assertTrue(preview.dividends[1]["_blocked"])

    def test_chat_defaults_are_part_of_identity_and_unknown_lines_are_visible(self):
        parser = self.importers().preview_text
        text = "삼성전자(005930) 매수 3주 70,000원\n알 수 없는 원본 문장"
        first = parser(text, default_account="A", default_date="2025-01-02")
        other_account = parser(text, default_account="B", default_date="2025-01-02")
        other_date = parser(text, default_account="A", default_date="2025-01-03")
        self.assertEqual(first.trades[0]["quantity"], "3")
        self.assertEqual(first.trades[0]["price"], "70000")
        self.assertFalse(first.trades[0]["_blocked"])
        self.assertNotEqual(first.trades[0]["id"], other_account.trades[0]["id"])
        self.assertNotEqual(first.trades[0]["id"], other_date.trades[0]["id"])
        self.assertTrue(any("알 수 없는 원본 문장" in issue for issue in first.issues))

    def test_chat_missing_amount_or_account_is_blocked(self):
        preview = self.importers().preview_text("2025-01-02 005930 매도 3주", default_date="2025-01-02")
        self.assertEqual(len(preview.trades), 1)
        self.assertTrue(preview.trades[0]["_blocked"])
        self.assertEqual(preview.trades[0]["price"], "")
        self.assertEqual(preview.trades[0]["account_id"], "")

    def test_generic_workbook_uses_header_mapping(self):
        book = Workbook()
        sheet = book.active
        sheet.title = "Trades"
        sheet.append(["거래일", "계좌", "종목코드", "매매구분", "수량", "단가"])
        sheet.append(["2025-02-03", "A", "AAPL", "매수", 2, 150.25])
        target = self.root / "generic.xlsx"
        book.save(target)
        book.close()
        preview = self.importers().preview_workbook(target)
        self.assertEqual(preview.trades[0]["instrument_id"], "US:AAPL")
        self.assertEqual(preview.trades[0]["currency"], "USD")
        self.assertEqual(preview.trades[0]["price"], "150.25")
        self.assertFalse(preview.trades[0]["_blocked"])

    def test_completed_legacy_preview_meets_persisted_domain_contract(self):
        from stockjournal.domain import TABLES, empty_state, validate_state

        preview = self.importers().preview_workbook(self.legacy_workbook(closed=True))
        state = empty_state()
        for table in TABLES:
            for row in getattr(preview, table):
                state[table][row["id"]] = {key: value for key, value in row.items() if not key.startswith("_")}
        validate_state(state)
        self.assertEqual(preview.dividend_events[0]["status"], "reference")

    def test_malformed_grouping_and_extreme_numbers_do_not_create_amounts(self):
        target = self.write_csv([
            ["account", "code", "date", "side", "quantity", "price"],
            ["A", "005930", "2025-01-02", "BUY", "1", "12,34"],
            ["A", "005930", "2025-01-02", "BUY", "1", "1e999"],
            ["A", "005930", "2025-01-02", "BUY", "1_000", "70000"],
        ])
        preview = self.importers().preview_csv(target)
        self.assertTrue(all(row["_blocked"] for row in preview.trades))
        self.assertEqual(preview.trades[0]["price"], "12,34")
        self.assertEqual(preview.trades[1]["price"], "1e999")
        self.assertEqual(preview.trades[2]["quantity"], "1_000")

    def test_six_character_korean_alphanumeric_code_is_never_integer_cast(self):
        target = self.write_csv([
            ["account", "code", "date", "side", "quantity", "price"],
            ["A", "A1234B", "2025-01-02", "BUY", "1", "70000"],
        ])
        preview = self.importers().preview_csv(target)
        self.assertEqual(preview.trades[0]["instrument_id"], "KR:A1234B")
        self.assertFalse(preview.trades[0]["_blocked"])

    def test_ambiguous_text_with_multiple_fills_is_blocked(self):
        preview = self.importers().preview_text("005930 매수 1주 70000원; 005930 매도 1주 75000원", default_account="A", default_date="2025-01-02")
        self.assertTrue(preview.trades[0]["_blocked"])
        self.assertTrue(preview.issues)

    def test_spreadsheet_cached_values_preserve_excel_epoch(self):
        from openpyxl.utils.datetime import MAC_EPOCH

        book = Workbook()
        book.epoch = MAC_EPOCH
        sheet = book.active
        sheet.title = "Trades"
        sheet.append(["date", "account", "code", "side", "quantity", "price"])
        sheet.append([1, "A", 5930, "BUY", 1, 70000])
        target = self.root / "mac_epoch.xlsx"
        book.save(target)
        book.close()
        preview = self.importers().preview_workbook(target)
        self.assertEqual(preview.trades[0]["executed_at"], "1904-01-02T00:00:00")
        self.assertEqual(preview.trades[0]["instrument_id"], "KR:005930")


if __name__ == "__main__":
    unittest.main()
