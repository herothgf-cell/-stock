"""Tk controls for local automatic simulation and explicitly armed execution.

The app prepares immutable context on Tk's thread; API work runs in one worker.
Workers never touch Tk, and stopping/closing sets the engine's stop event before
the UI can schedule another order leg.
"""
from __future__ import annotations

from decimal import Decimal
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from .automation import (AutomationBlocked, AutomationContext, AutomationEngine,
                         ExecutionJournal, ExecutionPolicy, TERMINAL, UNCERTAIN)


class AutomationPanel:
    def __init__(self, app, parent):
        self.app, self.root, self.parent = app, app.root, parent
        self.mode = tk.StringVar(value="DRY_RUN")
        self.cash_account_verified = tk.BooleanVar(value=False)
        self.max_order_usd = tk.StringVar(value="1000")
        self.max_buy_usd = tk.StringVar(value="1000")
        self.interval_seconds = tk.StringVar(value="60")
        self.status = tk.StringVar(value="모의 실행이 기본값입니다. 실주문은 계좌·예산을 확인하고 이 PC에서 직접 허용하세요.")
        self.running = False
        self.busy = False
        self.engine = None
        self.workflow_id = None
        self._selected_account = None
        self._options = None
        self._closed = False
        self._after_id = None
        self._poll_id = None
        self._cancel = threading.Event()
        self._control_lock = threading.Lock()
        self._results = queue.Queue()
        self._import_requested = set()
        try:
            self.journal = ExecutionJournal(app.directory / "execution-local.sqlite3")
        except AutomationBlocked as exc:
            self.journal = None
            self.status.set(str(exc))
        self._build()
        self.refresh()
        self._poll_id = self.root.after(150, self._poll)

    def _build(self):
        title = ttk.Frame(self.parent)
        title.pack(fill="x", pady=(2, 8))
        ttk.Label(title, text="200티큐 자동 실행", font=("Malgun Gothic", 14, "bold")).pack(side="left")
        ttk.Label(title, text="앱을 열어 둔 동안 실행 · 실행 상태는 이 PC에서만 관리", foreground="#536471").pack(side="right")
        controls = ttk.Frame(self.parent)
        controls.pack(fill="x", pady=4)
        ttk.Label(controls, text="방식").grid(row=0, column=0, padx=(0, 6), sticky="w")
        self.mode_box = ttk.Combobox(controls, textvariable=self.mode, values=("DRY_RUN", "LIVE"), width=11, state="readonly")
        self.mode_box.grid(row=0, column=1, sticky="w")
        self._entries = []
        for column, label, variable, width in ((2, "1회 주문 상한 USD", self.max_order_usd, 12),
                (4, "실행당 총 매수 상한 USD", self.max_buy_usd, 12),
                (6, "신호 점검 간격(초)", self.interval_seconds, 7)):
            ttk.Label(controls, text=label).grid(row=0, column=column, padx=(14, 6), sticky="w")
            entry = ttk.Entry(controls, textvariable=variable, width=width)
            entry.grid(row=0, column=column + 1, sticky="w")
            self._entries.append(entry)
        self.cash_check = ttk.Checkbutton(self.parent, text="선택한 계좌가 미국 주식 현금거래 계좌임을 확인했습니다. 신용·대출 가능 금액을 자동 매수 자금으로 쓰지 않습니다.",
            variable=self.cash_account_verified)
        self.cash_check.pack(anchor="w", pady=(7, 4))
        buttons = ttk.Frame(self.parent)
        buttons.pack(fill="x", pady=(3, 8))
        self.start_button = ttk.Button(buttons, text="선택 방식으로 자동 실행 시작", command=lambda: self._guard(self.start))
        self.start_button.pack(side="left")
        ttk.Button(buttons, text="새 주문 중지", command=self.stop).pack(side="left", padx=6)
        ttk.Button(buttons, text="NH 상태 대조 (조회만)", command=lambda: self._guard(self.reconcile)).pack(side="left")
        ttk.Button(buttons, text="대조 후 직접 재개", command=lambda: self._guard(self.resume)).pack(side="left", padx=6)
        ttk.Button(buttons, text="실체결 반영 다시 확인", command=lambda: self._guard(self.retry_import)).pack(side="left")
        ttk.Label(self.parent, text="지정가: 현재가 × 매수 1.001 / 매도 0.999 · 정수 주수 · 수수료 여유 0.5%. 매도 전량 체결과 실제 USD 현금 증가 후 다음 매수를 진행합니다.",
            foreground="#536471", wraplength=1120).pack(anchor="w", pady=(0, 7))
        ttk.Label(self.parent, textvariable=self.status, wraplength=1120, foreground="#174e62").pack(fill="x", pady=(0, 7))
        self.history = ttk.Treeview(self.parent, columns=("time", "mode", "status", "reason"), show="headings", height=4)
        for key, label, width in (("time", "시작 시각 (UTC)", 185), ("mode", "방식", 85), ("status", "상태", 145), ("reason", "확인 사항", 650)):
            self.history.heading(key, text=label)
            self.history.column(key, width=width, anchor="w")
        self.history.pack(fill="x")
        self.history.bind("<<TreeviewSelect>>", lambda event: self._show_selected())
        self.orders = ttk.Treeview(self.parent, columns=("symbol", "side", "quantity", "price", "status", "kind"), show="headings", height=5)
        for key, label, width in (("symbol", "종목", 100), ("side", "매매", 80), ("quantity", "주수", 80),
                ("price", "지정가 USD", 130), ("status", "주문 상태", 160), ("kind", "전략 단계", 250)):
            self.orders.heading(key, text=label)
            self.orders.column(key, width=width, anchor="w")
        self.orders.pack(fill="x", pady=8)
        self.log = tk.Text(self.parent, height=5, wrap="word", state="disabled", font=("Malgun Gothic", 9), background="#ffffff")
        self.log.pack(fill="both", expand=True)

    def _guard(self, function):
        try:
            function()
        except Exception as exc:
            messagebox.showerror("자동 실행 확인 필요", str(exc), parent=self.root)

    def _read_options(self):
        account_id = self.app.account_id()
        if not account_id:
            raise AutomationBlocked("전체 계좌가 아닌 실행 계좌 하나를 선택하세요.")
        mode = self.mode.get()
        if mode not in {"LIVE", "DRY_RUN"}:
            raise AutomationBlocked("DRY_RUN 또는 LIVE를 선택하세요.")
        try:
            maximum = Decimal(self.max_order_usd.get().strip())
            total = Decimal(self.max_buy_usd.get().strip())
            interval = int(self.interval_seconds.get().strip())
        except Exception as exc:
            raise AutomationBlocked("USD 상한과 점검 간격을 숫자로 입력하세요.") from exc
        if not maximum.is_finite() or not total.is_finite() or maximum <= 0 or total <= 0:
            raise AutomationBlocked("주문·매수 상한은 양수여야 합니다.")
        if not 30 <= interval <= 3600:
            raise AutomationBlocked("신호 점검 간격은 30~3600초입니다.")
        if mode == "LIVE" and not self.cash_account_verified.get():
            raise AutomationBlocked("실주문 전에 미국 현금거래 계좌 여부를 확인하세요.")
        return {"account_id": account_id, "mode": mode, "maximum": maximum, "total": total, "interval": interval}

    def _confirm_arm(self, options):
        if options["mode"] != "LIVE":
            return True
        name = self.app.state["accounts"][options["account_id"]].get("name", "선택 계좌")
        return messagebox.askyesno("이 PC에서 실주문 허용", f"{name}\n1회 주문 상한: ${options['maximum']}\n실행당 총 매수 상한: ${options['total']}\n\n"
            "앱을 열어 둔 동안 검증된 200티큐 신호에 따라 NH 지정가 주문을 제출합니다. "
            "알 수 없는 응답과 부분 체결은 자동 재주문하지 않습니다. "
            "다른 PC의 자동 실행은 중지해야 합니다.\n\n이 계좌와 금액으로 실주문을 허용할까요?", parent=self.root)

    def _set_running_controls(self):
        self.mode_box.configure(state="disabled" if self.running else "readonly")
        for entry in self._entries:
            entry.configure(state="disabled" if self.running else "normal")
        self.cash_check.configure(state="disabled" if self.running else "normal")
        self.start_button.configure(state="disabled" if self.running else "normal")

    def start(self):
        if self.busy or self.running:
            raise AutomationBlocked("현재 점검이 끝난 뒤 중지·시작을 선택하세요.")
        if self.journal is None:
            raise AutomationBlocked(self.status.get())
        options = self._read_options()
        if not self._confirm_arm(options):
            return
        self._options, self._selected_account = options, options["account_id"]
        self._cancel.clear()
        self.running = True
        self._set_running_controls()
        self.status.set("최신 신호·계좌·보유·현금·거래 세션을 확인하고 있습니다.")
        # Starting after a stop may observe a previous workflow, but never resumes it.
        self._dispatch("start", arm=True)

    def stop(self):
        self.running = False
        self._cancel.set()
        with self._control_lock:
            if self.engine:
                self.engine.stop()
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        if not self._closed:
            self._set_running_controls()
            self.status.set("새 주문을 중지했습니다. NH에 이미 접수된 주문은 별도로 체결될 수 있습니다. 상태 대조로 확인하세요.")

    def _policy(self, context, options):
        return ExecutionPolicy(options["account_id"], context.broker.account_id, options["maximum"], options["total"], mode=options["mode"])

    def _selected_workflow(self):
        selected = self.history.selection()
        identity = selected[0] if selected else self.workflow_id
        if not identity or not self.journal:
            raise AutomationBlocked("대조할 실행 기록을 선택하세요.")
        return self.journal.get(identity)

    def reconcile(self):
        if self.busy:
            raise AutomationBlocked("현재 점검이 끝난 뒤 대조하세요.")
        workflow = self._selected_workflow()
        if workflow["mode"] != "LIVE":
            raise AutomationBlocked("모의 결과에는 증권사 주문이 없습니다.")
        if self.app.account_id() != workflow["account_id"]:
            raise AutomationBlocked("기록의 원래 계좌를 선택한 뒤 대조하세요.")
        self.stop()
        self._dispatch("reconcile", workflow_id=workflow["id"])

    def resume(self):
        if self.busy:
            raise AutomationBlocked("현재 점검이 끝난 뒤 재개하세요.")
        workflow = self._selected_workflow()
        if workflow["mode"] != "LIVE" or workflow["status"] not in {"REVIEW_REQUIRED", "BLOCKED"}:
            raise AutomationBlocked("NH 상태를 대조한 실주문 실행을 선택하세요.")
        options = self._read_options()
        if options["mode"] != "LIVE" or options["account_id"] != workflow["account_id"]:
            raise AutomationBlocked("원래 계좌와 LIVE 방식을 선택하세요.")
        if not self._confirm_arm(options):
            return
        self._options, self._selected_account = options, options["account_id"]
        self._cancel.clear()
        self.running = True
        self._set_running_controls()
        self._dispatch("resume", arm=True, workflow_id=workflow["id"])

    def _dispatch(self, operation="tick", *, arm=False, workflow_id=None):
        if self._closed or self.busy:
            return
        if getattr(self.app, "busy", False):
            self.status.set("현재 조회·저장이 끝난 뒤 자동 실행 상태를 점검합니다.")
            if self.running and self._after_id is None:
                self._after_id = self.root.after(1000, self._scheduled)
            return
        current_id = workflow_id or self.workflow_id
        if operation == "reconcile":
            stored = self.journal.get(current_id)
            options = {"account_id": stored["account_id"], "mode": stored["mode"],
                "maximum": Decimal(stored["policy"]["max_order_usd"]),
                "total": Decimal(stored["policy"]["max_buy_usd"]), "interval": 60}
        else:
            options = dict(self._options or self._read_options())
        account_id = self.app.account_id()
        if options["account_id"] != account_id:
            self.stop()
            raise AutomationBlocked("선택 계좌가 바뀌어 자동 실행을 중지했습니다.")
        use_existing = operation in {"tick", "reconcile"} and current_id and self.engine is not None
        if use_existing and operation == "reconcile" and self.engine.broker.account_id != stored["broker_account_id"]:
            use_existing = False
        prepare = getattr(self.app, "prepare_automation_context", None)
        if not use_existing and not callable(prepare):
            raise AutomationBlocked("자동 실행에 필요한 NH·전략 연결이 준비되지 않았습니다.")
        broker_only = operation == "reconcile" and callable(getattr(self.app, "prepare_automation_broker", None))
        factory = None if use_existing else self.app.prepare_automation_broker() if broker_only else prepare()
        if factory is not None and not callable(factory):
            value = factory
            factory = lambda: value
        self.busy = True
        cancelled = self._cancel
        def worker():
            try:
                if use_existing:
                    result = self.engine.reconcile(current_id) if operation == "reconcile" else self.engine.tick(current_id)
                else:
                    context = factory()
                    if not broker_only and not isinstance(context, AutomationContext):
                        raise AutomationBlocked("자동 실행 자료 형식이 올바르지 않습니다.")
                    if cancelled.is_set() and operation not in {"reconcile"}:
                        self._results.put(("stopped", None))
                        return
                    broker = context if broker_only else context.broker
                    engine = self.engine or AutomationEngine(self.journal, broker)
                    with self._control_lock:
                        if cancelled.is_set() and operation != "reconcile":
                            self._results.put(("stopped", None))
                            return
                        self.engine = engine
                        self.engine.broker = broker
                        policy = None if broker_only else self._policy(context, options)
                        if arm:
                            if options["mode"] == "LIVE":
                                self.engine.arm(policy)
                            else:
                                self.engine.activate_dry_run()
                    if operation == "reconcile":
                        result = self.engine.reconcile(current_id)
                    elif operation == "resume":
                        result = self.engine.resume(current_id, evidence=context.evidence)
                    else:
                        pending = next((item for item in self.journal.list(10000) if item["mode"] == options["mode"] and
                            item["account_id"] == options["account_id"] and (item["status"] not in TERMINAL or
                                item["status"] == "COMPLETE" and not item.get("applied"))), None)
                        result = pending if pending else self.engine.start(context.decision, policy, context.evidence)
                        if result["status"] in {"RUNNING", "WAIT_CASH"}:
                            result = self.engine.tick(result["id"])
                self._results.put(("result", result))
            except Exception as exc:
                self._results.put(("error", str(exc) if isinstance(exc, AutomationBlocked) else "자동 실행 점검 실패: " + type(exc).__name__))
        threading.Thread(target=worker, name="stock-journal-automation", daemon=True).start()

    def _poll(self):
        if self._closed:
            return
        try:
            while True:
                kind, result = self._results.get_nowait()
                self.busy = False
                if kind == "error":
                    self.status.set(str(result))
                elif kind == "result":
                    self.workflow_id = result["id"]
                    self.refresh()
                    self.status.set(result["status"] + " · " + result.get("reason", ""))
                    if result["status"] == "COMPLETE" and not result.get("applied"):
                        self._request_import(result)
                    if result["status"] in {"NO_ACTION", "SIMULATED"} or result["status"] == "COMPLETE" and result.get("applied"):
                        self.workflow_id = None
                if self.running and self._after_id is None:
                    delay = 5000 if self.workflow_id else self._options["interval"] * 1000
                    self._after_id = self.root.after(delay, self._scheduled)
        except queue.Empty:
            pass
        self._poll_id = self.root.after(150, self._poll)

    def _scheduled(self):
        self._after_id = None
        if not self.running or self._closed:
            return
        try:
            if self.workflow_id:
                workflow = self.journal.get(self.workflow_id)
                if workflow["status"] == "COMPLETE" and workflow.get("applied"):
                    self.workflow_id = None
            self._dispatch()
        except Exception as exc:
            self.stop()
            self.status.set(str(exc))

    def _request_import(self, workflow):
        if getattr(self.app, "busy", False):
            self.status.set("현재 작업을 마친 뒤 실제 체결 원장을 반영합니다.")
            return
        if workflow["id"] in self._import_requested:
            return
        self._import_requested.add(workflow["id"])
        callback = getattr(self.app, "automation_fills", None)
        if not callable(callback):
            self.status.set("실제 체결을 원장에 가져와 전략 단계를 확인할 때까지 다음 자동 실행을 보류합니다.")
            return
        try:
            if callback(workflow) is True:
                self.engine.mark_applied(workflow["id"])
                self.refresh()
        except Exception as exc:
            self.status.set("실체결 원장 반영을 확인하세요: " + str(exc))

    def retry_import(self):
        workflow = self._selected_workflow()
        if workflow["status"] != "COMPLETE" or workflow["mode"] != "LIVE":
            raise AutomationBlocked("전량 체결한 실주문 실행을 선택하세요.")
        if self.app.account_id() != workflow["account_id"]:
            raise AutomationBlocked("원래 계좌를 선택한 뒤 실제 체결을 반영하세요.")
        self._import_requested.discard(workflow["id"])
        self._request_import(workflow)

    def refresh(self):
        if self._closed or self.journal is None:
            return
        if self.running and self._selected_account != self.app.account_id():
            self.stop()
            self.status.set("선택 계좌가 바뀌어 자동 실행을 중지했습니다.")
        selected = self.history.selection()
        selection = selected[0] if selected else self.workflow_id
        for item in self.history.get_children():
            self.history.delete(item)
        workflows = self.journal.list(50)
        for workflow in workflows:
            self.history.insert("", "end", iid=workflow["id"], values=(workflow["created_at"][:19], workflow["mode"],
                workflow["status"] + (" · 반영됨" if workflow.get("applied") else ""), workflow.get("reason", "")))
        if selection and self.history.exists(selection):
            self.history.selection_set(selection)
        elif workflows:
            self.history.selection_set(workflows[0]["id"])
        self._show_selected()

    def _show_selected(self):
        selected = self.history.selection()
        for item in self.orders.get_children():
            self.orders.delete(item)
        text = "모의 주문은 실제 체결·원가·전략 단계에 포함되지 않습니다."
        if selected and self.journal:
            workflow = self.journal.get(selected[0])
            for order in workflow["orders"]:
                self.orders.insert("", "end", values=(order["symbol"], order["side"], order.get("quantity") or "현금 확인 후 계산",
                    (order.get("request") or {}).get("limit_price", "현재가 확인 후 계산"), order["status"], order["kind"]))
            text = "\n".join(item["created_at"][:19] + "  " + item["event"] + "  " + item["detail"]
                             for item in self.journal.events(workflow["id"], 40))
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("1.0", text)
        self.log.configure(state="disabled")

    def close(self):
        """Return False only when the user keeps the stopped app open for review."""
        was_live = self._options and self._options["mode"] == "LIVE"
        self.stop()
        unresolved = [] if self.journal is None else [order for workflow in self.journal.list(10000)
            if workflow["mode"] == "LIVE" for order in workflow["orders"] if order["status"] in UNCERTAIN]
        if unresolved or self.busy and was_live:
            if not messagebox.askyesno("접수된 주문은 NH에서 계속 처리될 수 있습니다",
                "새 자동 주문은 중지했습니다. 아직 결과를 확인하지 못한 주문 또는 진행 중인 응답이 있습니다. "
                "앱을 닫아도 NH에 이미 접수된 주문이 취소되지는 않습니다.\n\n"
                "NH 앱에서 미체결·체결 내역을 확인해야 합니다. 이 상태를 확인하고 닫을까요?", parent=self.root):
                return False
        self._closed = True
        if self._poll_id is not None:
            try:
                self.root.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
        return True
