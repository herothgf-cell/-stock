"""Native strategy review page. All broker access here is read-only."""
from __future__ import annotations

import copy
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from . import strategy
from .ui import FormDialog, Table, money


LABELS = {"BUY_ENTRY": "분할 매수", "SELL_PROFIT": "부분 익절", "EXIT": "전량 정리", "BUY_SPYM": "익절대금 이동"}
RULES = (
    "적용 원문: 아기티큐 · 200티큐 행동 규칙 · 2026-05-07\n"
    "• TQQQ 정규장 종가의 200일선 상향 돌파: 1일차 현금 1/3 → 다음 거래일 남은 현금 1/2 → 다음 거래일 나머지. 각 종가가 선 위여야 합니다.\n"
    "• 종가가 선 아래: 분할 매수 중에도 TQQQ와 SPYM 전량 정리 → SGOV. 별도의 고점 대비 손절 규칙은 없습니다.\n"
    "• 환차손 포함 계좌 수익률 +10/+25/+50% 최초 도달마다 당시 남은 TQQQ의 10%, +100/+200/+300…%마다 50%를 익절 → SPYM. 같은 사이클에서 SPYM을 TQQQ로 되돌리지 않습니다.\n"
    "• 새 현금: TQQQ 보유·선 위에서는 SPYM 50% / SGOV 50%, 선 아래에서는 SGOV. 소액은 SPYM만 선택할 수 있습니다. 인출 순서: SGOV → SPYM → TQQQ.\n\n"
    "구현 가정: 분할 보정 200거래일 단순평균(SMA), 선과 같으면 보류, 매도 수량은 지정 단위로 사사오입(기본 1주), 매수는 예산 안에서 내림. 0주 단계는 자동 완료하지 않습니다. 여러 익절선 동시 통과는 순차 시나리오를 보여 주며 수동 판단이 필요합니다.\n"
    "NH 일봉은 분할 보정을 보장하지 않습니다. 자료 확인은 가격을 수정하지 않으므로, 실제 보정 자료 또는 기간 내 분할 부재를 확인해야 합니다. 2026~2028 미국 거래 달력의 실제 종료 10분 뒤부터 종가를 사용합니다(통상 뉴욕 16:10, 조기폐장 13:10). 임시 휴장·증권사 주문 가능 여부는 별도 확인합니다.\n"
    "첨부 Toss 3.96의 주문 저장·복구 구조는 별도 실행 계층의 참고입니다. 그 프로그램의 과열·고점 하락·대기일·익절 기본값을 이 원문 규칙으로 가져오지 않습니다.\n\n"
    "수동 체결 연결: 계획을 먼저 보관 → 증권사 체결을 매매 기록에 저장 → 보관 계획의 실제 체결을 연결. 보관 계획은 현재 앱 실행 동안만 유지합니다. 날짜만 있는 체결 기록은 정확한 장 마감 후 체결 시각을 사용자가 확인해야 합니다."
)


def read_close_csv(path):
    """Read only explicit date/close columns; adjustment still needs review."""
    path = Path(path)
    if path.stat().st_size > 5_000_000:
        raise ValueError("종가 CSV는 5MB 이하로 준비하세요.")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        names = {name.strip().casefold(): name for name in reader.fieldnames or []}
        date_key = next((names[name] for name in ("date", "날짜", "거래일") if name in names), None)
        close_key = next((names[name] for name in ("close", "종가", "수정종가") if name in names), None)
        if date_key is None or close_key is None:
            raise ValueError("UTF-8 CSV에 date,close 두 열(또는 날짜,종가)을 포함하세요.")
        rows = []
        for row in reader:
            if len(rows) >= 10000:
                raise ValueError("종가 CSV는 10,000행 이하로 준비하세요.")
            day, close = row.get(date_key), row.get(close_key)
            if not day or not close:
                raise ValueError("날짜 또는 종가가 비어 있는 행이 있습니다.")
            rows.append({"date": day.strip(), "close": close.replace(",", "").strip()})
    return {"symbol": "TQQQ", "currency": "USD", "session": "regular", "adjustment": "unknown",
            "finalization": "user_verified", "as_of": datetime.now(timezone.utc).isoformat(),
            "source": "사용자 CSV: " + path.name, "rows": rows,
            "warnings": ["CSV의 정규장 종가·분할 보정·최근 거래일을 원본 제공처에서 확인하세요."]}


class StrategyPage:
    """Integrates with JournalApp.account_id/work/save/provider and refresh."""

    def __init__(self, app, parent):
        self.app, self.parent = app, parent
        self.history = None
        self.decision = None
        self.plans = {}
        self.loaded_account = object()
        self.loaded_config = None
        self.cache_path = Path(app.directory) / "strategy-prices.json"
        try:
            if self.cache_path.exists() and self.cache_path.stat().st_size <= 5_000_000:
                self.history = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
        heading = ttk.Frame(parent)
        heading.pack(fill="x", pady=(0, 6))
        ttk.Label(heading, text="200티큐 · 계좌별 실행 계획", font=("Malgun Gothic", 16, "bold")).pack(side="left")
        ttk.Button(heading, text="원문 보기", command=lambda: webbrowser.open(strategy.SOURCE_URL)).pack(side="right")
        ttk.Label(parent, text="정규장 종가와 실제 원장을 사용합니다. 이 화면의 계획·체결 연결은 증권사 주문을 전송하지 않습니다.", foreground="#536471").pack(anchor="w", pady=(0, 8))
        self._buttons([( "NH TQQQ 종가 조회", self.fetch), ("종가 CSV 열기", self.open_csv), ("현재 가격 자료 확인", self.verify_prices), ("입력값 저장 · 재계산", self.save_inputs)])
        form = ttk.Frame(parent)
        form.pack(fill="x", pady=(0, 6))
        self.cash = tk.StringVar(value="0")
        self.step = tk.StringVar(value="1")
        self.roi = tk.StringVar(value="")
        self.roi_date = tk.StringVar(value="")
        self.pending_spym = tk.StringVar(value="0")
        self.costs_reviewed = tk.BooleanVar(value=False)
        for index, (label, variable, width) in enumerate((("분할 매수용 현금 USD", self.cash, 16), ("매매 수량 단위", self.step, 9), ("환차손 포함 수익률 %", self.roi, 12), ("수익률 미국 종가일", self.roi_date, 13))):
            ttk.Label(form, text=label).grid(row=0, column=index, sticky="w", padx=(0, 12))
            ttk.Entry(form, textvariable=variable, width=width).grid(row=1, column=index, sticky="ew", padx=(0, 12), pady=(3, 0))
            form.columnconfigure(index, weight=1)
        costs = ttk.Frame(parent)
        costs.pack(fill="x", pady=(0, 5))
        ttk.Label(costs, text="확인한 SPYM 대기금 USD").pack(side="left")
        self.pending_entry = ttk.Entry(costs, textvariable=self.pending_spym, width=16)
        self.pending_entry.pack(side="left", padx=(8, 16))
        ttk.Checkbutton(costs, text="실제 체결 비용·남은 예산을 확인했습니다", variable=self.costs_reviewed).pack(side="left")
        self.heading = tk.StringVar(value="계좌를 선택하세요")
        self.summary = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self.heading, font=("Malgun Gothic", 12, "bold")).pack(anchor="w", pady=(4, 2))
        ttk.Label(parent, textvariable=self.summary, foreground="#36566b", wraplength=1120).pack(fill="x", pady=(0, 7))
        self._buttons([("선택 계획 보관", self.keep_plan), ("보관 계획 체결 연결", self.connect_plan), ("기존 사이클 연결", self.adopt), ("사이클 종료", self.end_cycle)])
        panes = ttk.Panedwindow(parent, orient="vertical")
        panes.pack(fill="both", expand=True)
        self.actions = Table(panes, [("kind", "검토 항목", 110), ("symbol", "종목", 75), ("qty", "수량", 100),
                                     ("budget", "예산 USD", 120), ("level", "단계", 75), ("reason", "판단 근거", 580)], height=4)
        panes.add(self.actions, weight=2)
        detail = ttk.Frame(panes)
        self.details = tk.Text(detail, wrap="word", height=8, font=("Malgun Gothic", 10), background="white", relief="flat", padx=9, pady=8)
        scrollbar = ttk.Scrollbar(detail, orient="vertical", command=self.details.yview)
        self.details.configure(yscrollcommand=scrollbar.set)
        self.details.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        panes.add(detail, weight=3)

    def _buttons(self, buttons):
        bar = ttk.Frame(self.parent)
        bar.pack(fill="x", pady=(0, 7))
        for label, callback in buttons:
            ttk.Button(bar, text=label, command=lambda fn=callback: self.app.safe(fn)).pack(side="left", padx=(0, 6))

    def _account(self):
        uid = self.app.account_id()
        if uid is None:
            raise ValueError("상단에서 계좌 하나를 선택하세요.")
        return uid

    def _text(self, text):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def refresh(self):
        self.actions.clear()
        self.decision = None
        uid = self.app.account_id()
        if not uid:
            self.heading.set("상단에서 200티큐를 관리할 계좌 하나를 선택하세요")
            self.summary.set("계좌 사이에 현금·원가·익절 단계를 합산하지 않습니다.")
            self._text(RULES)
            self.loaded_account = None
            return
        config = self.app.state["accounts"][uid].get(strategy.FIELD, {})
        if uid != self.loaded_account or config != self.loaded_config:
            self.cash.set(config.get("cash_usd", "0"))
            self.step.set(config.get("quantity_step", "1"))
            saved = config.get("broker_return", {})
            self.roi.set(saved.get("pct", ""))
            self.roi_date.set(saved.get("date", ""))
            cycle = config.get("cycle") or {}
            self.pending_spym.set(cycle.get("pending_spym_usd", "0"))
            self.pending_entry.configure(state="normal" if cycle else "disabled")
            self.costs_reviewed.set(False)
            self.loaded_account, self.loaded_config = uid, copy.deepcopy(config)
        if not self.history:
            self.heading.set("정규장 종가 201개 이상을 불러오세요")
            self.summary.set("NH 조회 또는 date,close 형식 CSV · 가격 자료와 전략 설정은 개인 데이터로 저장됩니다.")
            self._text(RULES)
            return
        try:
            result = strategy.evaluate(self.app.state, uid, self.history)
        except (ValueError, TypeError, KeyError) as exc:
            self.heading.set("전략 입력을 확인하세요")
            self.summary.set(str(exc))
            self._text(RULES)
            return
        self.decision = result
        if not self.roi_date.get() and result.as_of:
            self.roi_date.set(result.as_of)
        self.heading.set(result.title)
        cycle = result.cycle
        levels = ", ".join(level + "%" for level in cycle["profit_levels"]) if cycle else ""
        stage = f"매수 {cycle['entry_count']}/3차 · 완료 익절 {levels or '없음'}" if cycle else "연결된 사이클 없음"
        holdings = " / ".join(f"{symbol} {money(value['quantity'], 6)}주" for symbol, value in result.positions.items())
        self.summary.set(f"미국 종가일 {result.as_of or '미조회'}   종가 ${money(result.close, 4)}   SMA200 ${money(result.sma, 4)}   계좌 수익률 {money(result.broker_return_pct, 2)}% (USD 참고 {money(result.usd_return_pct, 2)}%)\n{holdings}   ·   {stage}")
        for action in result.actions:
            self.actions.tree.insert("", "end", iid=action.id, values=(LABELS.get(action.kind, action.kind), action.symbol,
                "실제 시세 확인" if action.quantity is None else money(action.quantity, 6), money(action.budget_usd),
                f"+{action.threshold_pct}%" if action.threshold_pct is not None else f"{action.entry_number}/3" if action.entry_number else "", action.reason))
        messages = []
        if result.blockers:
            messages += ["신호 보류 사유", *["• " + item for item in result.blockers], ""]
        if result.warnings:
            messages += ["확인 사항", *["• " + item for item in result.warnings], ""]
        messages += ["자료: " + str(self.history.get("source", "출처 미표시")), "", RULES]
        self._text("\n".join(messages))

    def _received(self, data):
        from .app import write_json
        self.history = data
        write_json(self.cache_path, data)
        self.refresh()
        self.app.status.set(f"TQQQ 종가 {len(data.get('rows', []))}개를 조회했습니다. 분할 보정과 최신 거래일을 확인하세요.")

    def fetch(self):
        self._account()
        provider = self.app.provider()
        self.app.work("NH TQQQ 정규장 종가를 조회합니다…", lambda: provider.overseas_daily_prices("TQQQ", count=260), self._received)

    def open_csv(self):
        self._account()
        path = filedialog.askopenfilename(parent=self.app.root, title="TQQQ 정규장 종가 CSV", filetypes=[("CSV", "*.csv")])
        if path:
            self._received(read_close_csv(path))

    def verify_prices(self):
        uid = self._account()
        if not self.history:
            raise ValueError("먼저 종가 자료를 불러오세요.")
        text = ("현재 종가 자료 전체에 대해 다음 내용을 원본 제공처에서 확인했나요?\n\n"
                "• TQQQ의 미국 정규장 종가이며 마지막 날짜가 최근 완료 거래일입니다.\n"
                "• 최근 201거래일 이상이 빠짐없이 있고, 분할·병합을 일관되게 보정했거나 기간 중 분할이 없었습니다.\n\n"
                "이 확인은 실제 가격을 보정하지 않습니다. 자료가 바뀌면 다시 확인해야 합니다.")
        if messagebox.askyesno("가격 자료 확인", text, parent=self.app.root):
            expected = copy.deepcopy(self.app.state)
            self.app.save(strategy.confirm_history(expected, uid, self.history), expected=expected)

    def save_inputs(self):
        uid = self._account()
        expected = copy.deepcopy(self.app.state)
        target = strategy.update_preferences(expected, uid, cash_usd=self.cash.get().strip(), quantity_step=self.step.get().strip(),
            broker_return_pct=self.roi.get().strip() or None, return_date=self.roi_date.get().strip() or None,
            pending_spym_usd=self.pending_spym.get().strip() if expected["accounts"][uid].get(strategy.FIELD, {}).get("cycle") else None,
            costs_reviewed=self.costs_reviewed.get())
        self.app.save(target, expected=expected)

    def keep_plan(self):
        uid = self._account()
        selected = self.actions.selected()
        if not self.decision or not selected:
            raise ValueError("검토할 계획 행을 먼저 선택하세요.")
        action = next(item for item in self.decision.actions if item.id == selected)
        if action.quantity == 0:
            raise ValueError("0주 계획은 체결 완료로 표시할 수 없습니다.")
        self.plans[uid] = (copy.deepcopy(self.decision), selected)
        self.app.status.set("현재 앱 실행 동안 계획을 보관합니다. 실제 체결을 원장에 저장한 뒤 '보관 계획 체결 연결'을 누르세요.")

    def connect_plan(self):
        uid = self._account()
        if uid not in self.plans:
            self.keep_plan()
        decision, selected = self.plans[uid]
        action = next(item for item in decision.actions if item.id == selected)
        side = "BUY" if action.kind in ("BUY_ENTRY", "BUY_SPYM") else "SELL"
        available = []
        for row in self.app.active("trades"):
            instrument = self.app.state["instruments"][row["instrument_id"]]
            if row["account_id"] == uid and row["side"] == side and instrument["code"].upper() == action.symbol and row["id"] not in decision.existing_trade_ids:
                available.append(row)
        if not available:
            raise ValueError("계획을 보관했습니다. 해당 실제 체결을 매매 기록에 저장한 뒤 다시 연결하세요. 계획 보관 자체는 매매 기록을 만들지 않습니다.")
        dialog = tk.Toplevel(self.app.root)
        dialog.title("실제 체결과 계획 연결")
        dialog.transient(self.app.root)
        dialog.geometry("820x420")
        body = ttk.Frame(dialog, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"{decision.as_of} {LABELS[action.kind]} · {action.symbol} · 계획 {money(action.quantity, 6)}주\n연결할 실제 체결을 선택하세요. 복수 체결은 Ctrl을 누르고 선택합니다.", wraplength=760).pack(anchor="w", pady=(0, 8))
        table = Table(body, [("date", "체결일시", 210), ("side", "구분", 70), ("qty", "수량", 100), ("price", "단가 USD", 120), ("memo", "메모", 230)], height=8)
        table.tree.configure(selectmode="extended")
        table.pack(fill="both", expand=True)
        for row in sorted(available, key=lambda row: row["executed_at"], reverse=True):
            table.tree.insert("", "end", iid=row["id"], values=(row["executed_at"], row["side"], row["quantity"], row["price"], row.get("memo", "")))

        def confirm():
            expected = copy.deepcopy(self.app.state)
            target = strategy.confirm_action(expected, uid, decision, selected, table.tree.selection())
            self.app.save(target, expected=expected)
            self.plans.pop(uid, None)
            dialog.destroy()

        ttk.Button(body, text="선택한 실제 체결 연결", command=lambda: self.app.safe(confirm), style="Primary.TButton").pack(anchor="e", pady=(10, 0))
        dialog.grab_set()

    def adopt(self):
        uid = self._account()
        expected = copy.deepcopy(self.app.state)
        cycle = expected["accounts"][uid].get(strategy.FIELD, {}).get("cycle") or {}

        def save(values):
            target = strategy.adopt_cycle(expected, uid, start_date=values["start"], last_entry_date=values["last"],
                entry_count=values["entries"], profit_levels=[part.strip() for part in values["levels"].split(",") if part.strip()])
            self.app.save(target, expected=expected)

        FormDialog(self.app.root, "실제 보유분 사이클 연결", [
            ("start", "사이클 시작 미국 거래일", cycle.get("start_date", ""), None),
            ("last", "마지막 분할 매수 신호일", cycle.get("last_entry_date", ""), None),
            ("entries", "이미 완료한 분할 매수", cycle.get("entry_count", 3), {"1차 완료": 1, "2차 완료": 2, "3차 완료": 3}),
            ("levels", "이미 실행한 익절 % (쉼표 구분)", ",".join(cycle.get("profit_levels", [])), None),
        ], save, "원장의 실제 TQQQ 보유분에 이전 진행 단계를 연결합니다. 체결을 만들지 않습니다. 날짜는 YYYY-MM-DD, 완료 익절은 예: 10,25,50. 실제 기록을 확인한 값만 입력하세요.")

    def end_cycle(self):
        uid = self._account()
        expected = copy.deepcopy(self.app.state)
        target = strategy.reset_cycle(expected, uid)
        if messagebox.askyesno("사이클 종료", "원장의 TQQQ와 SPYM 잔고가 모두 0입니다. 이 사이클을 종료할까요? 체결 이력은 유지합니다.", parent=self.app.root):
            self.app.save(target, expected=expected)
            self.plans.pop(uid, None)
