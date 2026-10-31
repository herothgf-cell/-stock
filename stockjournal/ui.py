"""Reusable native widgets; form values are checked before callbacks run."""
from decimal import Decimal, InvalidOperation
import tkinter as tk
from tkinter import ttk, messagebox


def money(value, places=2):
    if value is None:
        return '미조회'
    try:
        n = Decimal(str(value)); text = f'{n:,.{places}f}'
        return text.rstrip('0').rstrip('.') if '.' in text else text
    except (InvalidOperation, ValueError):
        return str(value)


def number(value):
    raw = str(value).replace(',', '').strip()
    try:
        n = Decimal(raw)
        if not n.is_finite():
            raise ValueError()
        return format(n, 'f')
    except (InvalidOperation, ValueError):
        raise ValueError('금액·수량에 올바른 숫자를 입력하세요.') from None


class Table(ttk.Frame):
    def __init__(self, parent, columns, height=15):
        super().__init__(parent)
        self.tree = ttk.Treeview(self, columns=[c[0] for c in columns], show='headings', height=height, selectmode='browse')
        for key, label, width in columns:
            self.tree.heading(key, text=label); self.tree.column(key, width=width, minwidth=60, anchor='w')
        ys = ttk.Scrollbar(self, orient='vertical', command=self.tree.yview)
        xs = ttk.Scrollbar(self, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky='nsew'); ys.grid(row=0, column=1, sticky='ns'); xs.grid(row=1, column=0, sticky='ew')
        self.rowconfigure(0, weight=1); self.columnconfigure(0, weight=1)
        self.tree.tag_configure('blocked', foreground='#a33232', background='#fff0ee')

    def clear(self):
        self.tree.delete(*self.tree.get_children())

    def selected(self):
        selection = self.tree.selection()
        return selection[0] if selection else None


class FormDialog(tk.Toplevel):
    def __init__(self, parent, title, fields, callback, help_text=''):
        super().__init__(parent); self.title(title); self.transient(parent); self.resizable(True, False)
        body = ttk.Frame(self, padding=20); body.pack(fill='both', expand=True)
        variables = {}; mappings = {}
        if help_text:
            ttk.Label(body, text=help_text, wraplength=600, foreground='#536471').grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 14))
        for index, (key, label, value, choices) in enumerate(fields, 1):
            ttk.Label(body, text=label).grid(row=index, column=0, sticky='w', pady=5, padx=(0, 14))
            if choices is not None:
                mappings[key] = choices
                initial = next((k for k, v in choices.items() if v == value), '')
                variable = tk.StringVar(value=initial)
                widget = ttk.Combobox(body, textvariable=variable, values=list(choices), width=57)
                def filter_choices(event, box=widget, options=list(choices)):
                    if event.keysym in ('Up', 'Down', 'Return', 'Tab'):
                        return
                    query = box.get().casefold()
                    box.configure(values=[name for name in options if query in name.casefold()])
                widget.bind('<KeyRelease>', filter_choices)
            else:
                variable = tk.StringVar(value=str(value if value is not None else ''))
                widget = ttk.Entry(body, textvariable=variable, width=60)
            variables[key] = variable; widget.grid(row=index, column=1, sticky='ew', pady=5)
        def save():
            try:
                values = {}
                for key, variable in variables.items():
                    value = variable.get().strip()
                    if key in mappings:
                        if value not in mappings[key]:
                            raise ValueError('목록에서 정확한 계좌·종목·구분을 선택하세요.')
                        value = mappings[key][value]
                    values[key] = value
                callback(values); self.destroy()
            except Exception as exc:
                messagebox.showerror('입력 확인', str(exc), parent=self)
        actions = ttk.Frame(body); actions.grid(row=len(fields)+1, column=0, columnspan=2, sticky='e', pady=(16, 0))
        ttk.Button(actions, text='취소', command=self.destroy).pack(side='left', padx=5)
        ttk.Button(actions, text='저장', command=save, style='Primary.TButton').pack(side='left')
        body.columnconfigure(1, weight=1); self.grab_set()
