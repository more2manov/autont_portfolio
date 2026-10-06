# -*- coding: utf-8 -*-
"""
src/gui.py — окно менеджера (Tkinter), портфолио-версия.

Тот же модуль, что в рабочей версии, со следующими отличиями:
  * каталог читается из SQLite (db_sqlite.load_catalog, файл
    out/sealmatch.sqlite собирает build_mock.py), а не из PostgreSQL;
  * вкладка «Источники» показывает входные файлы демо (сид + мок-прайс),
    карантин и журнал изменений цен из служебных таблиц SQLite — вместо
    листов закрытых прайс-листов;
  * журнал заявок и выгрузки КП пишутся в out/ (в .gitignore);
  * кнопка «Пример заявки» и автоподбор аналогов при выделении варианта —
    чтобы демо проходилось без знания предметной области;
  * высота строк таблиц считается от шрифта (экраны с масштабом > 100%).
Ядро подбора (matching.py) вызывается так же, как в рабочей версии.

Копирование из таблиц: общий хелпер make_copy_menu(tree, mark_col) вешает
ПКМ-меню (ячейка/строка/маркировка) и Ctrl+C (маркировка) на все табличные
Treeview. Для таблиц каталога позиций mark_col — «Маркировка_в_прайсе» (или
«Маркировка» для КП); для сводных таблиц — естественный идентифицирующий
столбец («Группа», «SKU»).

SuppliersTab — соответствия «позиции каталога -> ВОЗМОЖНЫЕ поставщики»
(core.load_supplier_groups(), matching.group_row_mask). НЕ путать с колонкой
каталога «Поставщик» (источник цены строки, один на строку) — это отдельный
справочник для другой задачи.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from tkinter import font as tkfont

import core
import matching
import db_sqlite
import freshness
import history
import kp_export
from matching import (find_smart_analogs, find_cross_analogs, find_brand_analogs,
                      find_joint_analogs, find_cross_nominal_analogs)

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
OUT_DIR = PROJECT_ROOT / "out"   # КП и журнал заявок (в .gitignore)

COLOR_EXACT = "#DFF2DF"
COLOR_CROSS = "#FFE4D6"
COLOR_NOMINAL = "#F6D8EC"
COLOR_BRAND = "#FFF6D9"
COLOR_JOINT = "#E3E8FF"
COLOR_STALE = "#FFF3B0"
COLOR_NEAREST = "#EDE7F6"   # итерация 41: ближайшие варианты при промахе

# демо-заявка (кнопка «Пример заявки»): те же строки, что прогоняет build_mock.py
DEMO_REQUEST = [
    "СНП-Д-1-1-50-40 ГОСТ SS316L",
    "Набивка AX-101 8х8 — 12 кг",
    "DN80 PN16 SS321",
    'СНП-В 4" RF CL300 SS304 ASME',
    "Набивка AX 107 10x10",
]
# группы дерева точных позиций раскрываются сразу, если вариантов немного
AUTO_OPEN_ROWS = 40

RESULT_COLS = ["SKU", "Маркировка_в_прайсе", "Стандарт", "DN / Размер",
               "PN / Давление", "Толщина / Высота", "Материал",
               "Вес_кг", "Остаток", "Цена_без_НДС", "Поставщик", "Источник", "Координата",
               "Ограничитель"]


# ============================ ОБЩИЕ УТИЛИТЫ ДЛЯ TREEVIEW ============================

_NAN_STRINGS = {"nan", "none"}

# человекочитаемое имя файла-источника в таблицах результата; сырые коды
# SEED/PRICE остаются в данных как есть — по ним навигирует вкладка
# «Источники», поэтому подменяются только на отображении.
SOURCE_LABELS = {"SEED": "mock_catalog_seed.json", "PRICE": "mock_prices.xlsx"}


def _fmt(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    s = str(v)
    if s.strip().lower() in _NAN_STRINGS:
        return ""
    return s


def _fmt_cell(row, col):
    """Как _fmt(row.get(col)), но «Источник» отображается человекочитаемо
    (имя входного файла), а «Ограничитель» (итерация 27)
    вычисляется хелпером matching.restrictor_kind — сама колонка в данных
    не меняется (обе — только рендер)."""
    if col == "Ограничитель":
        return matching.restrictor_kind(row)
    v = _fmt(row.get(col))
    if col == "Толщина / Высота" and v:
        # итерация 37b: у СНП ГОСТ В/Г/Д рядом с высотой витой части (F) — толщина
        # кольца (G, «ring=» в размерах); только рендер, данные/КП/фильтр — по F
        m = re.search(r"ring=([0-9.]+)", _fmt(row.get("Основные_размеры")))
        if m:
            return f"{v} (кольцо {m.group(1)})"
    if col == "Источник":
        return SOURCE_LABELS.get(v, v)
    return v


def _kp_request(cust):
    """Итерация 50: срез cust строки заявки для kp_export (маркировка клиента
    и давление заявки); None — позиция добавлена не из строки заявки."""
    if not cust:
        return None
    keys = ("raw", "pn", "pn_lo", "pn_hi", "class_lo", "class_hi")
    req = {k: cust.get(k) for k in keys}
    req["route"] = matching.request_route(cust)
    return req


def _kp_catalog_range(row):
    lo, hi, _rated = matching.row_pressure_kgs(row)
    return {"catalog_kgs_lo": lo, "catalog_kgs_hi": hi}


def _suppliers_label(n: int) -> str:
    """«(N поставщиков)» с русским склонением: 2-4 -> поставщика, иначе поставщиков."""
    m10, m100 = n % 10, n % 100
    word = "поставщика" if (2 <= m10 <= 4 and not 12 <= m100 <= 14) else "поставщиков"
    return f"({n} {word})"


def _add_hscroll(tree: ttk.Treeview):
    """Нижняя горизонтальная прокрутка (итерация 35). Вызывать ПОСЛЕ pack
    дерева; stretch=False фиксирует текущие ширины колонок — иначе Treeview
    сжимает их под ширину окна и прокручивать нечего."""
    for c in ("#0",) + tuple(tree["columns"]):
        tree.column(c, stretch=False)
    hsb = ttk.Scrollbar(tree.master, orient="horizontal", command=tree.xview)
    tree.configure(xscrollcommand=hsb.set)
    hsb.pack(side="bottom", fill="x", before=tree)
    return hsb


def _bind_mousewheel(widget, tree: ttk.Treeview):
    """Прокрутка колесом мыши (Windows: delta кратен 120)."""
    widget.bind("<MouseWheel>", lambda e: tree.yview_scroll(-int(e.delta / 120), "units"))


def make_copy_menu(tree: ttk.Treeview, mark_col: str) -> None:
    """ПКМ по строке -> меню «Копировать ячейку/строку/маркировку»;
    Ctrl+C на выделенной строке копирует маркировку (колонку mark_col).
    Буфер обмена — через root.clipboard_clear()/clipboard_append(),
    без messagebox; статус — необязательная подсказка в status-строке
    ближайшего предка вкладки, если она там есть."""
    menu = tk.Menu(tree, tearoff=0)
    ctx = {"iid": None, "col_idx": None}

    def _status(msg):
        w = tree
        for _ in range(8):
            w = getattr(w, "master", None)
            if w is None:
                return
            if hasattr(w, "status"):
                try:
                    w.status.config(text=msg)
                except Exception:
                    pass
                return

    def _copy(text):
        root = tree.winfo_toplevel()
        root.clipboard_clear()
        root.clipboard_append(text)

    def copy_cell():
        iid, col_idx = ctx["iid"], ctx["col_idx"]
        if iid is None or col_idx is None:
            return
        vals = tree.item(iid, "values")
        text = str(vals[col_idx]) if col_idx < len(vals) else ""
        _copy(text)
        _status(f"Скопировано: {text}")

    def copy_row():
        if ctx["iid"] is None:
            return
        text = "\t".join(str(v) for v in tree.item(ctx["iid"], "values"))
        _copy(text)
        _status("Строка скопирована в буфер обмена")

    def copy_mark(iid=None):
        iid = iid if iid is not None else ctx["iid"]
        if iid is None:
            return
        cols = list(tree["columns"])
        if mark_col not in cols:
            return
        idx = cols.index(mark_col)
        vals = tree.item(iid, "values")
        text = str(vals[idx]) if idx < len(vals) else ""
        _copy(text)
        _status(f"Маркировка скопирована: {text}")

    def on_right_click(event):
        iid = tree.identify_row(event.y)
        if not iid:
            return
        tree.selection_set(iid)
        ctx["iid"] = iid
        col = tree.identify_column(event.x)
        col_idx = None
        if col and col.startswith("#"):
            n = int(col[1:]) - 1
            cols = list(tree["columns"])
            if 0 <= n < len(cols):
                col_idx = n
        ctx["col_idx"] = col_idx
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def on_ctrl_c(event=None):
        sel = tree.selection()
        if not sel:
            return
        copy_mark(sel[0])

    def on_key_ctrl_c(event):
        # Ctrl+C по ФИЗИЧЕСКОЙ клавише (event.keycode), а не по keysym: под
        # нелатинской раскладкой (русской и т.п.) физическая клавиша "C" даёт
        # keysym, отличный от "c", и bind("<Control-c>") молча не срабатывает —
        # это и есть баг «копирование не работает у менеджера» (хотфикс 9.2).
        # keycode 67 = 'C' на Windows независимо от активной раскладки.
        if (event.state & 0x0004) and event.keycode == 67:
            on_ctrl_c(event)

    menu.add_command(label="Копировать ячейку", command=copy_cell)
    menu.add_command(label="Копировать строку (через таб)", command=copy_row)
    menu.add_command(label="Копировать маркировку", command=copy_mark)

    tree.bind("<Button-3>", on_right_click)
    tree.bind("<Control-c>", on_ctrl_c)
    tree.bind("<Key>", on_key_ctrl_c, add="+")


_CLIPBOARD_KEYCODES = {86: "paste", 67: "copy", 88: "cut", 65: "select_all"}
_CLIPBOARD_KEYSYMS = {"v", "c", "x", "a"}


def _bind_clipboard_keys(root):
    """Ctrl+V/C/X/A независимо от раскладки клавиатуры (итерация 13) — тот же
    класс бага, что Ctrl+C в Treeview (хотфикс 9.2): под нелатинской
    раскладкой физическая клавиша V/C/X/A даёт keysym, отличный от "v"/"c"/
    "x"/"a", и штатные keysym-биндинги Tk для Text/Entry молча не срабатывают.

    Ловим <Key> на root — bindtags любого потомка (Text/Entry/Treeview везде
    в приложении) включают toplevel, поэтому событие сюда доходит, если
    инстанс-биндинг виджета не вернул "break". Действуем ТОЛЬКО если keysym
    ещё не латинский (значит, штатный Tk-биндинг не сработает сам) — под
    латинской раскладкой это условие ложно и наш код не вмешивается вообще,
    поведение буквально не меняется. И только для фокусного tk.Text/Entry —
    Treeview обслуживает make_copy_menu, этот код её не трогает (правит
    только Text/Entry, класс виджета которых первым проверяется ниже).

    Возвращает сам обработчик (не только биндит) — Tk на Windows не умеет
    синтезировать событие с произвольным «чужим» keysym через event_generate
    (нет активной нелатинской раскладки ОС -> "no keycode for keysym"), а
    OS-автоматизация переключения раскладки запрещена; возвращённый
    обработчик проверяется напрямую вызовом с поддельным event-объектом."""
    def on_key(event):
        if not (event.state & 0x0004):
            return
        action = _CLIPBOARD_KEYCODES.get(event.keycode)
        if action is None or event.keysym.lower() in _CLIPBOARD_KEYSYMS:
            return
        w = root.focus_get()
        if not isinstance(w, (tk.Text, tk.Entry, ttk.Entry)):
            return
        is_text = isinstance(w, tk.Text)
        try:
            if action == "paste":
                text = root.clipboard_get()
                if (is_text and w.tag_ranges("sel")) or (not is_text and w.selection_present()):
                    w.delete("sel.first", "sel.last")
                w.insert("insert", text)
            elif action in ("copy", "cut"):
                sel = w.get("sel.first", "sel.last") if is_text else w.selection_get()
                root.clipboard_clear()
                root.clipboard_append(sel)
                if action == "cut":
                    w.delete("sel.first", "sel.last")
            elif action == "select_all":
                if is_text:
                    w.tag_add("sel", "1.0", "end-1c")
                else:
                    w.selection_range(0, "end")
        except tk.TclError:
            pass
        return "break"

    root.bind("<Key>", on_key, add="+")
    return on_key


def fill_tree(tree: ttk.Treeview, df: pd.DataFrame, cols, tag=None):
    tree.delete(*tree.get_children())
    item_row = {}
    for idx, row in df.iterrows():
        vals = [_fmt(row.get(c)) for c in cols]
        iid = tree.insert("", "end", values=vals, tags=(tag,) if tag else ())
        item_row[iid] = row
    return item_row


# ============================ ВКЛАДКА 1: ПОДБОР ЗАЯВОК ============================

class OrderTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.exact_rows = {}     # iid ребёнка -> (row, cust, line_no)
        self.exact_parents = set()  # iid родителей (групп) — итерация 13
        self.nearest_rows = {}   # iid ребёнка «ближайших вариантов» -> (row, cust, line_no), итерация 41
        self.analog_rows = {}    # iid -> (row, line_no)
        self.line_cust = {}      # № строки заявки -> cust (итерация 50: давление/маркировка для КП)
        self.kp_items = []       # list of dict: sku, mark, qty, price, line_no, is_analog, reason
        self.last_misses = []    # list of (line, diag) — для «Почему не найдено?»

        self.banner = tk.Label(self, text="", anchor="w", padx=6, pady=3)
        self._banner_bg = self.banner.cget("background")
        self.banner.pack(fill="x")

        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=4)
        ttk.Label(top, text="Заявки (по одной строке, например: «СНП-Д-1-1-50-40 ГОСТ SS316L»"
                             " или «Набивка AX-101 8х8 — 12 кг»):").pack(anchor="w")
        self.lines_txt = tk.Text(top, height=5)
        self.lines_txt.pack(fill="x")

        btns = ttk.Frame(top)
        btns.pack(fill="x", pady=4)
        ttk.Button(btns, text="Подобрать", command=self.on_resolve).pack(side="left")
        ttk.Button(btns, text="Пример заявки",
                   command=self.insert_demo).pack(side="left", padx=6)
        self.btn_why = ttk.Button(btns, text="Почему не найдено?",
                                   command=self._show_why, state="disabled")
        self.btn_why.pack(side="left", padx=6)
        ttk.Button(btns, text="Аналоги (для выделенной строки)",
                   command=self.on_analogs).pack(side="left", padx=6)
        ttk.Button(btns, text="Импорт заявки…",
                   command=self._import_request).pack(side="left", padx=6)
        ttk.Button(btns, text="История…",
                   command=self._show_history).pack(side="left", padx=6)

        mid = ttk.PanedWindow(self, orient="vertical")
        mid.pack(fill="both", expand=True, padx=6, pady=4)

        exact_frame = ttk.LabelFrame(mid, text="Точные позиции")
        # show="tree headings" (итерация 13): #0 — дерево «строка заявки ->
        # варианты», сворачивается по группам, удобно для многострочных
        # заявок (9 строк заявки давали 72 плоские строки таблицы).
        exact_cols = RESULT_COLS + ["Причина"]   # «Причина» — только у ближайших вариантов
        self.exact_tree = ttk.Treeview(exact_frame, columns=exact_cols,
                                        show="tree headings", height=10)
        self.exact_tree.heading("#0", text="Позиция заявки")
        self.exact_tree.column("#0", width=380, anchor="w")
        for c in exact_cols:
            self.exact_tree.heading(c, text=c)
            self.exact_tree.column(c, width=110, anchor="w")
        self.exact_tree.pack(fill="both", expand=True)
        _add_hscroll(self.exact_tree)
        self.exact_tree.tag_configure("exact", background=COLOR_EXACT)
        self.exact_tree.tag_configure("group", background="#EAEAEA")
        self.exact_tree.tag_configure("nearest", background=COLOR_NEAREST)
        self.exact_tree.tag_configure("nearest_group", background="#D9CDEE")
        self.exact_tree.bind("<Double-1>", self._on_exact_dblclick)
        # портфолио: выделение варианта сразу считает его аналоги (в рабочей
        # версии — только кнопкой «Аналоги»)
        self.exact_tree.bind("<<TreeviewSelect>>", self._on_exact_select, add="+")
        make_copy_menu(self.exact_tree, "Маркировка_в_прайсе")
        mid.add(exact_frame, weight=1)

        analog_cols = RESULT_COLS + ["Причина"]
        analog_frame = ttk.LabelFrame(mid, text="Аналоги")
        self.analog_tree = ttk.Treeview(analog_frame, columns=analog_cols,
                                         show="headings", height=8)
        for c in analog_cols:
            self.analog_tree.heading(c, text=c)
            self.analog_tree.column(c, width=110, anchor="w")
        self.analog_tree.pack(fill="both", expand=True)
        _add_hscroll(self.analog_tree)
        self.analog_tree.tag_configure("smart", background="#FFFFFF")
        self.analog_tree.tag_configure("cross", background=COLOR_CROSS)
        self.analog_tree.tag_configure("nominal", background=COLOR_NOMINAL)
        self.analog_tree.tag_configure("brand", background=COLOR_BRAND)
        self.analog_tree.tag_configure("joint", background=COLOR_JOINT)
        self.analog_tree.bind("<Double-1>", self._on_analog_dblclick)
        make_copy_menu(self.analog_tree, "Маркировка_в_прайсе")
        mid.add(analog_frame, weight=1)

        kp_frame = ttk.LabelFrame(self, text="Коммерческое предложение")
        kp_frame.pack(fill="both", expand=False, padx=6, pady=4)
        kp_cols = ["SKU", "Маркировка", "Поз. заявки", "Кол-во",
                   "Цена без НДС", "Сумма без НДС", "Примечание"]
        self.kp_tree = ttk.Treeview(kp_frame, columns=kp_cols, show="headings", height=6)
        for c in kp_cols:
            self.kp_tree.heading(c, text=c)
            self.kp_tree.column(c, width=140, anchor="w")
        self.kp_tree.pack(fill="both", expand=True, side="left")
        _add_hscroll(self.kp_tree)
        self.kp_tree.bind("<Double-1>", self._on_kp_dblclick)
        # итерация 50 (находка 3): строка, у которой только что выросло кол-во
        self.kp_tree.tag_configure("merged", background="#FFE699")
        make_copy_menu(self.kp_tree, "Маркировка")

        kp_btns = ttk.Frame(kp_frame)
        kp_btns.pack(side="left", fill="y", padx=6)
        ttk.Button(kp_btns, text="Убрать строку", command=self._kp_remove).pack(fill="x", pady=2)
        ttk.Button(kp_btns, text="КП в Excel", command=self._export_xlsx).pack(fill="x", pady=2)

        self.status = ttk.Label(self, text="")
        self.status.pack(fill="x", padx=6, pady=2)

        self._refresh_freshness_banner()

    def _refresh_freshness_banner(self):
        """Баннер устаревания (итерация 11): при старте вкладки и при каждом
        «Подобрать» — не блокирует подбор, только предупреждает."""
        try:
            fresh = freshness.check_freshness(self.app.db_path)
        except Exception:
            return
        if fresh.get("stale"):
            self.banner.config(
                text=f"{fresh.get('reason')} — пересоберите: python build_mock.py",
                bg=COLOR_STALE)
        else:
            self.banner.config(text="", bg=self._banner_bg)

    def insert_demo(self):
        """Вставить демо-заявку (DEMO_REQUEST) и сразу подобрать."""
        self.lines_txt.delete("1.0", "end")
        self.lines_txt.insert("1.0", "\n".join(DEMO_REQUEST))
        self.on_resolve()

    def _on_exact_select(self, _event=None):
        sel = self.exact_tree.selection()
        if len(sel) == 1 and sel[0] in self.exact_rows:
            self.on_analogs()

    def _current_lines(self):
        return [ln.strip() for ln in self.lines_txt.get("1.0", "end").splitlines()
                if ln.strip()]

    # ---------- точный подбор ----------
    def on_resolve(self):
        self._refresh_freshness_banner()
        lines = self._current_lines()
        self.exact_tree.delete(*self.exact_tree.get_children())
        self.exact_rows.clear()
        self.exact_parents.clear()
        self.nearest_rows.clear()
        self.kp_items.clear()          # <-- новоe: КП начинается с чистого листа
        self._redraw_kp()              # <-- новоe
        misses = []
        self.last_misses = []
        nearest_groups = []  # (line_no, line, cust, DataFrame) — итерация 41
        items = []          # (line_no, line, row) — вход matching.group_by_line
        line_cust = {}      # line_no -> cust (кол-во по умолчанию при добавлении в КП)
        self.line_cust = line_cust
        for line_no, line in enumerate(lines, start=1):
            cust = matching.parse_customer_line(line)
            line_cust[line_no] = cust
            # как в entry.py cmd_search: явная маркировка/тип СНП или слово «СНП» ->
            # точный подбор resolve_customer; иначе (например, марка набивки) ->
            # нечёткий поиск search_generic, иначе resolve_customer без единого
            # ограничения молча вернул бы весь каталог.
            routed_resolve = cust.get("mark") or cust.get("type") or "СНП" in line.upper()
            # предохранитель маршрутизации (хотфикс 9.2): «СНП» в тексте есть,
            # но парсер не извлёк ни одного признака — resolve_customer(cust)
            # без единого фильтра отдал бы весь каталог; сначала пробуем
            # нечёткий поиск, и только потом считаем строку промахом.
            no_criteria = bool(routed_resolve) and not matching.has_criteria(cust)
            # итерация 18 (SELECTION_SPEC A12): без маркировки/типа/«СНП», но
            # с распознанными DN/PN/сталью/толщиной — search_generic не видит
            # эти признаки как текст (топ-причина промахов аудита боевого
            # журнала), а resolve_customer не подходит (нет категории-якоря).
            use_criteria_search = not routed_resolve and matching.has_dn_pn_steel_thickness(cust)
            if routed_resolve and not no_criteria:
                res = matching.resolve_customer(self.app.df, cust)
                # guard: вырожденный df без колонки «Категория» — защита от
                # падений (root-cause исправлен в matching.pressure_mask, но
                # guard остаётся дополнительным рубежом).
                if len(res) and "Категория" in res.columns:
                    res = res[res["Категория"].fillna("").astype(str) == "СНП"]
                elif len(res):
                    res = res.iloc[0:0]
            elif use_criteria_search:
                res = matching.search_with_criteria(self.app.df, line, cust)
            else:
                res = matching.search_generic(self.app.df, line)
            if not len(res):
                misses.append(line)
                if no_criteria:
                    diag = "маркировка/параметры не распознаны"
                elif routed_resolve or use_criteria_search:
                    diag = matching.diagnose_miss(self.app.df, cust)
                else:
                    # R2 (итерация 13, закрытие SELECTION_SPEC §3): промах
                    # search-пути -> токенная диагностика (ненайденные токены
                    # + топ-3 кандидата), а не dummy «причина не определена».
                    diag = matching.diagnose_search(self.app.df, line)
                self.last_misses.append((line, diag or "причина не определена"))
                if not routed_resolve:
                    # итерация 41 (SELECTION_SPEC §18): промах остаётся промахом
                    # (misses/журнал), но ниже строка получает ближайшие варианты
                    nn = matching.nearest_variants(self.app.df, line, cust)
                    if len(nn):
                        nearest_groups.append((line_no, line, cust, nn))
                continue
            # приоритет для менеджера (итерация 10): наличие -> своё
            # производство -> цена; только точная таблица, аналоги сохраняют
            # свою каскадную сортировку (см. _append_analogs).
            res = matching.sort_for_manager(res)
            for _, row in res.iterrows():
                items.append((line_no, line, row))

        # единственный источник группировки — matching.group_by_line (итерация
        # 13): ключ (line_no, line), а не текст маркировки — две строки заявки
        # с одинаковой маркировкой, но разным номером (например, одна и та же
        # позиция на разное давление), дают ДВЕ разные группы.
        groups = matching.group_by_line(items)
        open_groups = len(items) <= AUTO_OPEN_ROWS
        total_hits = 0
        dup_count = 0
        # итерация 21: детект дублей групп — разные строки заявки могут
        # резолвиться в ОДИН и тот же набор позиций каталога (например,
        # давление 25 и 16 кгс попадают в один диапазон каталожной строки
        # «10.0–160.0» — см. SELECTION_SPEC, раздел о семантике дублей).
        # Отпечаток группы — набор SKU её детей (без учёта порядка); первое
        # вхождение отпечатка — не дубль, все следующие — дубли ПЕРВОГО.
        seen_fp: dict = {}   # frozenset(SKU) -> № строки заявки первого вхождения
        for g in groups:
            g_line_no, g_line, rows = g["line_no"], g["line"], g["rows"]
            cust = line_cust.get(g_line_no, {})
            total_hits += len(rows)
            fp = frozenset(str(r.get("SKU")) for r in rows)
            dup_of = seen_fp.get(fp)
            if fp not in seen_fp:
                seen_fp[fp] = g_line_no
            g["dup_of"] = dup_of
            if dup_of is not None:
                dup_count += 1
            prices = pd.to_numeric(
                pd.Series([r.get("Цена_без_НДС") for r in rows]), errors="coerce").dropna()
            if len(prices):
                pmin, pmax = float(prices.min()), float(prices.max())
                price_txt = f"{pmin:.2f}" if pmin == pmax else f"{pmin:.2f}–{pmax:.2f}"
            else:
                price_txt = ""
            stocks = pd.to_numeric(
                pd.Series([r.get("Остаток") for r in rows]), errors="coerce").dropna()
            stock_txt = str(int(stocks.sum())) if len(stocks) else ""
            parent_vals = {c: "" for c in RESULT_COLS}
            parent_vals["Маркировка_в_прайсе"] = g_line
            parent_vals["Цена_без_НДС"] = price_txt
            parent_vals["Остаток"] = stock_txt
            parent_text = f"[{g_line_no}] {g_line} — {len(rows)} вариантов"
            if len(rows) > 10:
                # итерация 20: широкие группы (десятки вариантов, типично у
                # search_with_criteria — критерии DN/PN сами по себе не
                # сужают до одной категории) плохо читаются одним числом;
                # топ-3 категории по числу строк — быстрая ориентировка без
                # раскрытия дерева. Детей/сортировку/агрегаты не трогаем.
                cat_counts: dict = {}
                for r in rows:
                    cat = str(r.get("Категория") or "")
                    cat_counts[cat] = cat_counts.get(cat, 0) + 1
                top3 = sorted(cat_counts.items(), key=lambda kv: -kv[1])[:3]
                parent_text += " (" + ", ".join(f"{c}: {n}" for c, n in top3) + ")"
            if dup_of is not None:
                parent_text += f" — дубль строки {dup_of}"
            parent_iid = self.exact_tree.insert(
                "", "end", text=parent_text,
                values=[parent_vals[c] for c in RESULT_COLS],
                open=open_groups, tags=("group",))
            self.exact_parents.add(parent_iid)
            for row in rows:
                vals = [_fmt_cell(row, c) for c in RESULT_COLS]
                iid = self.exact_tree.insert(parent_iid, "end", values=vals, tags=("exact",))
                self.exact_rows[iid] = (row, cust, g_line_no)

        # итерация 41: строки без точного совпадения — родитель «✗ нет точного
        # совпадения» + ближайшие варианты (развёрнуты; вне отпечатка дублей —
        # в groups они не входят, вне суммы, добавляются в КП как аналоги)
        nearest_total = 0
        for n_line_no, n_line, n_cust, nn in nearest_groups:
            p_vals = {c: "" for c in RESULT_COLS}
            p_vals["Маркировка_в_прайсе"] = n_line
            p_iid = self.exact_tree.insert(
                "", "end",
                text=f"[{n_line_no}] {n_line} — ✗ нет точного совпадения — "
                     f"ближайшие варианты: {len(nn)}",
                values=[p_vals[c] for c in RESULT_COLS],
                open=True, tags=("nearest_group",))
            self.exact_parents.add(p_iid)
            for _, row in nn.iterrows():
                vals = [_fmt_cell(row, c) for c in RESULT_COLS] + [_fmt(row.get("Причина"))]
                iid = self.exact_tree.insert(p_iid, "end", values=vals, tags=("nearest",))
                self.nearest_rows[iid] = (row, n_cust, n_line_no)
            nearest_total += len(nn)

        msg = (f"Точных позиций: {total_hits} из {len(lines)} строк заявки "
               f"({len(groups)} групп, дублей: {dup_count}).")
        if nearest_total:
            msg += f" Ближайших вариантов: {nearest_total}."
        if misses:
            msg += " Без точного совпадения: " + "; ".join(misses)
            msg += f" Причина (первая): {self.last_misses[0][1]}"
        self.status.config(text=msg)
        self.btn_why.config(state=("normal" if self.last_misses else "disabled"))

        if lines:
            # журнал заявок (итерация 15): одна запись на «Подобрать»;
            # совпало — строки, нашедшие хоть один вариант (группы), а не
            # число самих вариантов-детей.
            try:
                history.append_entry(lines, hits=len(groups), misses=len(misses))
            except Exception:
                pass

    def _show_why(self):
        if not self.last_misses:
            return
        text = "\n".join(f"{line} → {diag}" for line, diag in self.last_misses)
        messagebox.showinfo("Почему не найдено?", text)

    # ---------- импорт заявки из файла ----------
    def _import_request(self):
        path = filedialog.askopenfilename(
            title="Импорт заявки",
            filetypes=[("Excel/CSV", "*.xlsx *.xlsm *.csv"), ("Все файлы", "*.*")])
        if not path:
            return
        try:
            lines = matching.parse_request_file(path)
        except Exception as e:
            messagebox.showerror("Импорт заявки", f"Не удалось прочитать файл: {e}")
            return
        if not lines:
            messagebox.showinfo("Импорт заявки", "В файле не найдено строк заявки.")
            return
        if self.lines_txt.get("1.0", "end").strip():
            if not messagebox.askyesno("Импорт заявки",
                                        "Поле заявок не пусто. Заменить содержимое?"):
                return
        self.lines_txt.delete("1.0", "end")
        self.lines_txt.insert("1.0", "\n".join(lines))
        self.on_resolve()
        name = Path(path).name
        self.status.config(text=self.status.cget("text")
                            + f"  импортировано {len(lines)} строк из {name}")

    # ---------- журнал заявок ----------
    def _show_history(self):
        entries = history.read_journal(30)
        win = tk.Toplevel(self)
        win.title("История заявок")
        win.geometry("760x420")
        cols = ["Дата", "Первая строка", "Итог", "КП"]
        tree = ttk.Treeview(win, columns=cols, show="headings")
        widths = {"Дата": 140, "Первая строка": 340, "Итог": 160, "КП": 110}
        for c in cols:
            tree.heading(c, text=c)
            tree.column(c, width=widths[c], anchor="w")
        vsb = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        _add_hscroll(tree)
        _bind_mousewheel(tree, tree)
        make_copy_menu(tree, "Первая строка")

        row_entry = {}
        for e in reversed(entries):   # новые сверху
            e_lines = e.get("lines") or []
            first_line = e_lines[0] if e_lines else ""
            summary = f"{len(e_lines)} строк, совпало {e.get('hits', 0)}"
            kp = e.get("kp_file") or "—"
            iid = tree.insert("", "end", values=[
                freshness.format_ts(e.get("ts")), first_line, summary, kp])
            row_entry[iid] = e

        def on_dblclick(event):
            iid = tree.identify_row(event.y)
            if not iid or iid not in row_entry:
                return
            e_lines = row_entry[iid].get("lines") or []
            self.lines_txt.delete("1.0", "end")
            self.lines_txt.insert("1.0", "\n".join(e_lines))
            win.destroy()
            self.on_resolve()

        tree.bind("<Double-1>", on_dblclick)

    # ---------- аналоги ----------
    def on_analogs(self):
        sel = self.exact_tree.selection()
        self.analog_tree.delete(*self.analog_tree.get_children())
        self.analog_rows.clear()
        child_sel = [iid for iid in sel if iid in self.exact_rows]
        if sel and not child_sel:
            # выделена(ы) только группа(ы) целиком, ни одного ребёнка
            # (итерация 13: дерево точных позиций) — аналоги считаются для
            # КОНКРЕТНОГО варианта, не для всей группы сразу.
            messagebox.showinfo(
                "Аналоги", "Выделена группа целиком — раскройте её (двойной "
                           "клик) и выделите конкретный вариант, чтобы "
                           "подобрать для него аналоги.")
            return
        if child_sel:
            # аналоги для выбранного(ых) варианта(ов): стык (та же Стандарт,
            # семья типов) + кросс-стандарт tier 1 «посадка» + tier 2
            # «номинал» (итерация 28: тот же DN_num в другом стандарте, вне
            # допуска CROSS_TOL — tier 1 всегда сверху, tier 2 сразу за ним,
            # find_cross_nominal_analogs сам исключает строки, уже отданные
            # tier 1) + бренд (итерация 26: joint — первым, он специфичнее
            # «кросс-стандарта» и не требует смены Стандарт)
            n = n_tier1 = n_tier2 = 0
            for iid in child_sel:
                row, cust, line_no = self.exact_rows[iid]
                joint = find_joint_analogs(self.app.df, row)
                n += self._append_analogs(joint, "joint", line_no)
                cross = find_cross_analogs(self.app.df, row)
                n_tier1 += self._append_analogs(cross, "cross", line_no)
                nominal = find_cross_nominal_analogs(self.app.df, row)
                n_tier2 += self._append_analogs(nominal, "nominal", line_no)
                brand = find_brand_analogs(self.app.df, row)
                n += self._append_analogs(brand, "brand", line_no)
            n += n_tier1 + n_tier2
            if n == 0:
                # итерация 23: пустая таблица аналогов раньше молчала («: 0»
                # без объяснения) — диагностика (ближайшие кандидаты другого
                # стандарта + причина отклонения) вместо кнопки: сразу видна
                # менеджеру в статус-строке первой же строкой. Итерация 28:
                # «0» означает, что ПУСТЫ ОБА уровня (посадка и номинал) —
                # diagnose_analogs (прежний вывод, не менялся) остаётся
                # единственным источником объяснения.
                first_row, _cust, _line_no = self.exact_rows[child_sel[0]]
                diag = matching.diagnose_analogs(self.app.df, first_row)
                first_line = diag.split("\n", 1)[0] if diag else ""
                suffix = f" — {first_line}" if first_line else ""
                self.status.config(text=f"аналогов: 0 (посадка: 0, номинал: 0){suffix}")
            else:
                self.status.config(
                    text=f"аналогов: {n} (посадка: {n_tier1}, номинал: {n_tier2})")
            return
        # иначе — каскадный подбор по последней разобранной строке заявки
        lines = self._current_lines()
        if not lines:
            messagebox.showinfo("Аналоги", "Введите заявку или выделите точную позицию.")
            return
        line_no = len(lines)
        line = lines[-1]
        cust = matching.parse_customer_line(line)
        if not (cust.get("mark") or cust.get("type") or "СНП" in line.upper()):
            messagebox.showinfo(
                "Аналоги", "Для этой строки (не СНП) выделите точную позицию "
                           "в верхней таблице и снова нажмите «Аналоги» — "
                           "покажутся кросс-стандартные/брендовые аналоги.")
            return
        if not matching.has_criteria(cust):
            # итерация 26: строка распознана как «СНП» (routed), но парсер не
            # извлёк ни одного признака (репро: «СНП-А-Е-Г-15-40» — буквенная
            # пара не входит ни в один известный стык, см. JOINT_MARK_RE) —
            # без этого guard'а find_smart_analogs(cust) с пустым cust
            # молча каскадировал бы весь каталог как «аналоги».
            self.status.config(text=f"[{line}] параметры не распознаны — аналоги недоступны")
            return
        res, note = find_smart_analogs(self.app.df, cust)
        n = self._append_analogs(res, "smart", line_no)
        self.status.config(text=f"[{line}] {note}")

    def _append_analogs(self, df, tag, line_no=None):
        if df is None or not len(df):
            return 0
        cols = RESULT_COLS + ["Причина"]
        n = 0
        for _, row in df.iterrows():
            vals = [_fmt_cell(row, c) for c in cols]
            iid = self.analog_tree.insert("", "end", values=vals, tags=(tag,))
            self.analog_rows[iid] = (row, line_no)
            n += 1
        return n

    # ---------- КП ----------
    def _on_exact_dblclick(self, event):
        iid = self.exact_tree.identify_row(event.y)
        if not iid:
            return
        if iid in self.exact_parents:
            # двойной клик по родителю (группе) — раскрыть/свернуть, а не
            # добавлять в КП (итерация 13: дерево точных позиций).
            self.exact_tree.item(iid, open=not self.exact_tree.item(iid, "open"))
            return
        if iid in self.nearest_rows:
            # итерация 41: тот же путь, что у аналогов (NULL-цена, дубль SKU)
            row, cust, line_no = self.nearest_rows[iid]
            self._add_to_kp(row, cust.get("qty", 1), line_no=line_no, is_analog=True,
                            reason="ближайший вариант — требует подтверждения", cust=cust)
            return
        if iid not in self.exact_rows:
            return
        row, cust, line_no = self.exact_rows[iid]
        self._add_to_kp(row, cust.get("qty", 1), line_no=line_no, cust=cust)

    def _on_analog_dblclick(self, event):
        iid = self.analog_tree.identify_row(event.y)
        if not iid or iid not in self.analog_rows:
            return
        row, line_no = self.analog_rows[iid]
        self._add_to_kp(row, 1, line_no=line_no, is_analog=True,
                         reason=row.get("Причина", ""), cust=self.line_cust.get(line_no))

    def _add_to_kp(self, row, qty, line_no=None, is_analog=False, reason="", cust=None):
        # итерация 31: у части брендовых аналогов (Источник BRAND_EQUIV_MD)
        # цена не задана («цена по запросу») — позиция годится для подбора
        # аналога, но не для КП; тихая коэрсия NaN->0.0 ниже раньше пускала
        # бы такую строку в КП бесплатной.
        price_raw = pd.to_numeric(pd.Series([row.get("Цена_без_НДС")]),
                                   errors="coerce").iloc[0]
        if pd.isna(price_raw):
            messagebox.showinfo(
                "Цена не задана",
                "Цена не задана — позиция только для подбора аналога.")
            return
        qty = simpledialog.askinteger("Количество", "Кол-во:", initialvalue=qty or 1,
                                       minvalue=1, parent=self)
        if not qty:
            return
        price = float(price_raw)
        item = dict(sku=row.get("SKU"), mark=row.get("Маркировка_в_прайсе"),
                    standard=row.get("Стандарт"), dn=row.get("DN / Размер"),
                    pn=row.get("PN / Давление"), thickness=row.get("Толщина / Высота"),
                    material=row.get("Материал"), price=price,
                    # итерация 34: три поля — только для kp_export (лист
                    # «Служебный»/matching.restrictor_kind); на существующий
                    # рендер панели КП (_redraw_kp) не влияют.
                    product_type=row.get("Тип_изделия"), source=row.get("Источник"),
                    coord=row.get("Координата"),
                    line_no=(line_no if line_no is not None else "—"),
                    is_analog=is_analog, reason=(reason if is_analog else ""),
                    # итерация 50 (находка 4): строка заявки и её давление —
                    # kp_export.kp_name/kp_pressure (маркировка клиента в КП)
                    category=row.get("Категория"),
                    request=_kp_request(cust),
                    **_kp_catalog_range(row))
        # итерация 21: защита от дублей в КП — повторное добавление SKU,
        # уже присутствующего в предложении (например, ребёнок группы-дубля,
        # см. детект выше), по умолчанию МОЛЧА создавало бы вторую строку с
        # тем же товаром. Без дубля — прежнее поведение, без диалога.
        existing = next((it for it in self.kp_items if it.get("sku") == item["sku"]), None)
        if existing is not None:
            if not messagebox.askyesno(
                    "Слияние в КП",
                    f"SKU {item['sku']} уже в КП (позиция {existing.get('line_no', '—')}): "
                    f"суммировать количество?"):
                return
        before = existing.get("qty") if existing is not None else None
        self.kp_items, action = matching.merge_or_append_kp(self.kp_items, item, qty)
        merged_sku = None
        if action == "merge":
            # итерация 50 (находка 3): повторное добавление складывает кол-во
            # (задумано, итерация 21) — теперь это видно менеджеру
            after = next(it["qty"] for it in self.kp_items if it.get("sku") == item["sku"])
            merged_sku = item["sku"]
            self.status.config(text=f"КП: {item['sku']} — количество увеличено: "
                                    f"было {before}, стало {after}")
        self._redraw_kp(highlight_sku=merged_sku)

    def _kp_remove(self):
        sel = self.kp_tree.selection()
        if not sel:
            return
        idx = self.kp_tree.index(sel[0])
        del self.kp_items[idx]
        self._redraw_kp()

    def _on_kp_dblclick(self, event):
        iid = self.kp_tree.identify_row(event.y)
        if not iid:
            return
        idx = self.kp_tree.index(iid)
        item = self.kp_items[idx]
        qty = simpledialog.askinteger("Количество", "Новое кол-во:",
                                       initialvalue=item["qty"], minvalue=1, parent=self)
        if qty:
            item["qty"] = qty
            self._redraw_kp()

    def _redraw_kp(self, highlight_sku=None):
        self.kp_tree.delete(*self.kp_tree.get_children())
        for it in self.kp_items:
            s = it["qty"] * it["price"]
            tags = ("merged",) if highlight_sku is not None and it["sku"] == highlight_sku else ()
            self.kp_tree.insert("", "end", values=[
                it["sku"], kp_export.kp_name(it), it.get("line_no", "—"), it["qty"],
                f"{it['price']:.2f}", f"{s:.2f}", it.get("reason", "")], tags=tags)

    def _export_xlsx(self):
        if not self.kp_items:
            messagebox.showinfo("КП в Excel", "Панель КП пуста — добавьте позиции двойным кликом.")
            return
        import datetime

        sheets = kp_export.build_kp_sheets(
            self.kp_items, datetime.date.today().strftime("%d.%m.%Y"))

        out_dir = OUT_DIR / "КП"
        out_dir.mkdir(parents=True, exist_ok=True)
        nums = [int(m.group(1))
                for m in (re.search(r"КП_(\d+)", p.name)
                          for p in out_dir.glob("КП_*.xlsx")) if m]
        next_n = (max(nums) + 1) if nums else 1
        out_path = out_dir / f"КП_{next_n:03d}.xlsx"
        kp_export.write_xlsx(sheets, out_path)   # оформление — итерация 50

        # журнал заявок (итерация 15): привязать КП к записи, у которой то
        # же поле заявок, что сейчас (обычный случай — КП собирают сразу
        # после «Подобрать», без правки поля); если поле уже изменили или
        # запись не найдена — mark_kp тихо ничего не делает (не ошибка).
        try:
            history.mark_kp(self._current_lines(), out_path.name)
        except Exception:
            pass

        messagebox.showinfo("КП в Excel", f"Сохранено: {out_path}")


# ============================ ВКЛАДКА 2: КАТАЛОГ ============================

CATALOG_COLS = ["SKU", "Категория", "Тип_изделия", "Стандарт", "Маркировка_в_прайсе",
               "DN / Размер", "PN / Давление", "Материал", "Толщина / Высота",
               "Вес_кг", "Остаток", "Цена_без_НДС", "Поставщик", "Источник", "Координата"]
PAGE_SIZE = 100


class CatalogTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.sort_col = None
        self.sort_desc = False
        self.page = 0

        filt = ttk.Frame(self)
        filt.pack(fill="x", padx=6, pady=4)

        df = app.df
        cats = [""] + sorted(x for x in df["Категория"].dropna().unique())
        stds = [""] + sorted(x for x in df["Стандарт"].dropna().unique() if str(x).strip())
        mats = [""] + sorted(x for x in df["Материал"].dropna().unique() if str(x).strip())

        ttk.Label(filt, text="Категория:").pack(side="left")
        self.cb_cat = ttk.Combobox(filt, values=cats, width=14, state="readonly")
        self.cb_cat.set("")
        self.cb_cat.pack(side="left", padx=(2, 10))

        ttk.Label(filt, text="Стандарт:").pack(side="left")
        self.cb_std = ttk.Combobox(filt, values=stds, width=10, state="readonly")
        self.cb_std.set("")
        self.cb_std.pack(side="left", padx=(2, 10))

        ttk.Label(filt, text="Материал:").pack(side="left")
        self.cb_mat = ttk.Combobox(filt, values=mats, width=16, state="readonly")
        self.cb_mat.set("")
        self.cb_mat.pack(side="left", padx=(2, 10))

        ttk.Label(filt, text="DN:").pack(side="left")
        self.e_dn = ttk.Entry(filt, width=6)
        self.e_dn.pack(side="left", padx=(2, 10))

        ttk.Label(filt, text="Поиск (маркировка/SKU):").pack(side="left")
        self.e_search = ttk.Entry(filt, width=20)
        self.e_search.pack(side="left", padx=(2, 10))

        self.only_in_stock = tk.BooleanVar(value=False)
        ttk.Checkbutton(filt, text="только в наличии", variable=self.only_in_stock,
                        command=self.apply_filters).pack(side="left", padx=(2, 10))

        ttk.Button(filt, text="Применить", command=self.apply_filters).pack(side="left", padx=4)
        ttk.Button(filt, text="Сброс", command=self.reset_filters).pack(side="left")

        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True, padx=6, pady=4)
        self.tree = ttk.Treeview(tree_frame, columns=CATALOG_COLS, show="headings")
        for c in CATALOG_COLS:
            self.tree.heading(c, text=c, command=lambda c=c: self.sort_by(c))
            self.tree.column(c, width=110, anchor="w")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        _add_hscroll(self.tree)
        _bind_mousewheel(self.tree, self.tree)
        make_copy_menu(self.tree, "Маркировка_в_прайсе")
        # итерация 36: метка «(N поставщиков)» в колонке «Поставщик» — гиперссылка
        self.groups = core.load_supplier_groups()
        self._link_gid = {}
        self.tree.tag_configure("supplier_link", foreground="#0645ad")
        self.tree.bind("<Button-1>", self._on_tree_click, add="+")
        self.tree.bind("<Motion>", self._on_tree_motion, add="+")

        pager = ttk.Frame(self)
        pager.pack(fill="x", padx=6, pady=2)
        ttk.Button(pager, text="‹", width=3, command=self.prev_page).pack(side="left")
        self.lbl_page = ttk.Label(pager, text="")
        self.lbl_page.pack(side="left", padx=8)
        ttk.Button(pager, text="›", width=3, command=self.next_page).pack(side="left")

        self.filtered = df
        self.apply_filters()

    def reset_filters(self):
        self.cb_cat.set("")
        self.cb_std.set("")
        self.cb_mat.set("")
        self.e_dn.delete(0, "end")
        self.e_search.delete(0, "end")
        self.only_in_stock.set(False)
        self.apply_filters()

    def set_supplier_filter(self, supplier: str):
        self.reset_filters()
        self._supplier_filter = supplier
        self.apply_filters()

    def apply_filters(self):
        df = self.app.df
        f = df
        cat = self.cb_cat.get()
        std = self.cb_std.get()
        mat = self.cb_mat.get()
        dn = self.e_dn.get().strip()
        q = self.e_search.get().strip()
        supplier = getattr(self, "_supplier_filter", None)

        if cat:
            f = f[f["Категория"] == cat]
        if std:
            f = f[f["Стандарт"] == std]
        if mat:
            f = f[f["Материал"] == mat]
        if dn:
            try:
                dn_val = float(dn)
                f = f[pd.to_numeric(f["DN_num"], errors="coerce") == dn_val]
            except ValueError:
                pass
        if q:
            hay = (f["Маркировка_в_прайсе"].fillna("").astype(str) + " "
                   + f["SKU"].fillna("").astype(str))
            # без пробелов, как в matching.search_generic: «МС101» обязан
            # находить «МС 101 24х24» (маркировка с пробелом), не только
            # позиции с уже слитной записью.
            hay_ns = hay.str.replace(" ", "", regex=False)
            q_ns = q.replace(" ", "")
            mask = (hay.str.contains(q, case=False, na=False, regex=False)
                    | hay_ns.str.contains(q_ns, case=False, na=False, regex=False))
            f = f[mask]
        if supplier:
            f = f[f["Поставщик"].astype(str).str.contains(re.escape(supplier), na=False)]
        if self.only_in_stock.get():
            f = f[pd.to_numeric(f["Остаток"], errors="coerce") > 0]

        self.filtered = f
        self.page = 0
        self._apply_sort()
        self._redraw()

    def sort_by(self, col):
        if self.sort_col == col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col, self.sort_desc = col, False
        self._apply_sort()
        self.page = 0
        self._redraw()

    def _apply_sort(self):
        if self.sort_col is None:
            return
        s = pd.to_numeric(self.filtered[self.sort_col], errors="coerce")
        if s.notna().sum() >= len(self.filtered) * 0.5:
            key = s
        else:
            key = self.filtered[self.sort_col].astype(str)
        self.filtered = self.filtered.assign(_sortkey=key).sort_values(
            "_sortkey", ascending=not self.sort_desc, na_position="last").drop(columns=["_sortkey"])

    def _redraw(self):
        n = len(self.filtered)
        pages = max(1, (n + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page = min(self.page, pages - 1)
        start = self.page * PAGE_SIZE
        chunk = self.filtered.iloc[start:start + PAGE_SIZE]
        self.tree.delete(*self.tree.get_children())
        self._link_gid = {}
        cover = matching.suppliers_for_frame(chunk, self.groups)
        sup_i = CATALOG_COLS.index("Поставщик")
        for (_, row), (sups, first_gid) in zip(chunk.iterrows(), cover):
            vals = [_fmt_cell(row, c) for c in CATALOG_COLS]
            tags = ()
            if len(sups) >= 2:
                vals[sup_i] = _suppliers_label(len(sups))
                tags = ("supplier_link",)
            elif len(sups) == 1 and not vals[sup_i].strip():
                vals[sup_i] = sups[0]
            iid = self.tree.insert("", "end", values=vals, tags=tags)
            if tags:
                self._link_gid[iid] = first_gid
        self.lbl_page.config(text=f"Стр. {self.page + 1} / {pages}  (строк: {n})")

    def _link_at(self, event):
        """iid строки, если (x, y) попал в ячейку-метку «Поставщик»."""
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        iid = self.tree.identify_row(event.y)
        if iid not in self._link_gid:
            return None
        col = self.tree.identify_column(event.x)
        if self.tree.column(col, "id") != "Поставщик":
            return None
        return iid

    def _on_tree_motion(self, event):
        self.tree.configure(cursor="hand2" if self._link_at(event) else "")

    def _on_tree_click(self, event):
        iid = self._link_at(event)
        if iid is not None:
            self.open_supplier_group(self._link_gid[iid])

    def open_supplier_group(self, gid):
        """Переключить на «Поставщики», выделить группу gid и открыть drill-down."""
        self.app.notebook.select(self.app.suppliers_tab)
        self.app.suppliers_tab.select_group(gid)

    def prev_page(self):
        if self.page > 0:
            self.page -= 1
            self._redraw()

    def next_page(self):
        n = len(self.filtered)
        pages = max(1, (n + PAGE_SIZE - 1) // PAGE_SIZE)
        if self.page < pages - 1:
            self.page += 1
            self._redraw()


# ============================ ВКЛАДКА 3: ПОСТАВЩИКИ ============================
# Итерация 33: собственная копия маски правил группы удалена — SuppliersTab
# вызывает matching.group_row_mask напрямую (single-source, см. его
# докстринг; до этой итерации здесь жила своя копия конъюнкции, отстававшая
# от entry.py/matching.py — не поддерживала standards/material_not и была бы
# четвёртой копией, которую пришлось бы держать в курсе mark_not).


class SuppliersTab(ttk.Frame):
    """Слева — группы справочника supplier_groups (title, поставщики через
    запятую, число совпавших позиций каталога); справа — совпавшие позиции
    выбранной группы (SKU/Маркировка/Категория/Материал/текущий Поставщик).
    Только чтение, других вкладок не касается."""

    POS_COLS = ["SKU", "Маркировка_в_прайсе", "Категория", "Материал", "Поставщик"]
    _SHOW_LIMIT = 300
    # итерация 33: виртуальная первая «группа» — не запись supplier_groups.
    # json, а ревизионная поверхность по литералу core.SUPPLIER_UNDEFINED
    # («поставщик не определён» — СНП/Ленты/часть Кольца КГН до оверрайдов
    # v4); drill-down показывает ВСЕ колонки каталога, а не только POS_COLS.
    UNDEFINED_ID = "__undefined__"

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.groups = core.load_supplier_groups()
        self._masks = {}

        # итерация 46: разделитель между списком групп и таблицей позиций —
        # тянется мышью, таблица позиций расширяется вправо/влево
        self.paned = ttk.Panedwindow(self, orient="horizontal")
        self.paned.pack(fill="both", expand=True, padx=6, pady=6)
        left = ttk.Frame(self.paned)
        self.paned.add(left, weight=0)
        ttk.Label(left, text="Группы поставщиков:").pack(anchor="w")
        # итерация 45: группы с 0 позиций скрыты по умолчанию (данные не меняются)
        self.show_empty = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="показать пустые группы", variable=self.show_empty,
                        command=self._refill_groups).pack(anchor="w")
        grp_cols = ["Группа", "Поставщики", "Позиций"]
        self.group_tree = ttk.Treeview(left, columns=grp_cols, show="headings", height=28)
        for c, w in zip(grp_cols, (240, 220, 70)):
            self.group_tree.heading(c, text=c)
            self.group_tree.column(c, width=w, anchor="w")
        vsb1 = ttk.Scrollbar(left, orient="vertical", command=self.group_tree.yview)
        self.group_tree.configure(yscrollcommand=vsb1.set)
        self.group_tree.pack(side="left", fill="y")
        vsb1.pack(side="left", fill="y")
        _add_hscroll(self.group_tree)
        self.group_tree.bind("<<TreeviewSelect>>", self._on_select)
        _bind_mousewheel(self.group_tree, self.group_tree)
        make_copy_menu(self.group_tree, "Группа")

        right = ttk.Frame(self.paned)
        self.paned.add(right, weight=1)
        self.status = ttk.Label(right, text="Выберите группу слева.")
        self.status.pack(anchor="w")
        self.pos_tree = ttk.Treeview(right, columns=self.POS_COLS, show="headings")
        for c in self.POS_COLS:
            self.pos_tree.heading(c, text=c)
            self.pos_tree.column(c, width=160, anchor="w")
        vsb2 = ttk.Scrollbar(right, orient="vertical", command=self.pos_tree.yview)
        self.pos_tree.configure(yscrollcommand=vsb2.set)
        self.pos_tree.pack(side="left", fill="both", expand=True)
        vsb2.pack(side="left", fill="y")
        _add_hscroll(self.pos_tree)
        _bind_mousewheel(self.pos_tree, self.pos_tree)
        make_copy_menu(self.pos_tree, "SKU")

        self._build_groups()
        if not self.groups:
            self.status.config(text="Справочник групп поставщиков пуст: секция supplier_groups "
                                    "в data/mock_suppliers.json не найдена.")

    def _build_groups(self):
        df = self.app.df
        undef_mask = df["Поставщик"].fillna("").astype(str) == core.SUPPLIER_UNDEFINED
        self._masks[self.UNDEFINED_ID] = undef_mask
        self._group_counts = {}
        for g in self.groups:
            mask = matching.group_row_mask(df, g["rules"])
            self._masks[g["id"]] = mask
            self._group_counts[g["id"]] = int(mask.sum())
        self._refill_groups()

    def _refill_groups(self):
        """Заполнить список групп; пустые (0 позиций) — только при флажке."""
        df = self.app.df
        sel = self.group_tree.selection()
        self.group_tree.delete(*self.group_tree.get_children())
        undef_mask = self._masks[self.UNDEFINED_ID]
        share = int(undef_mask.sum()) / len(df) if len(df) else 0.0
        self.group_tree.insert("", "end", iid=self.UNDEFINED_ID, values=[
            "[ревизия] поставщик не определён", "-",
            f"{int(undef_mask.sum())} ({share:.1%})"])
        for g in self.groups:
            n = self._group_counts[g["id"]]
            if n == 0 and not self.show_empty.get():
                continue
            self.group_tree.insert("", "end", iid=g["id"], values=[
                g["title"], ", ".join(g["suppliers"]),
                "(0 позиций)" if n == 0 else n])
        keep = [i for i in sel if self.group_tree.exists(i)]
        if keep:
            self.group_tree.selection_set(keep)
        elif sel:
            self._on_select()

    def select_group(self, gid):
        """Выделить группу в списке слева и открыть её drill-down (итерация 36)."""
        if gid not in self._masks:
            return
        if not self.group_tree.exists(gid):
            self.show_empty.set(True)  # пустая группа скрыта — показать
            self._refill_groups()
        self.group_tree.selection_set(gid)
        self.group_tree.focus(gid)
        self.group_tree.see(gid)
        self._on_select()

    def _set_pos_columns(self, cols):
        if list(self.pos_tree["columns"]) == list(cols):
            return
        self.pos_tree["columns"] = cols
        width = 160 if cols is self.POS_COLS else 110
        for c in cols:
            self.pos_tree.heading(c, text=c)
            self.pos_tree.column(c, width=width, anchor="w")

    def _on_select(self, _event=None):
        for i in self.pos_tree.get_children():
            self.pos_tree.delete(i)
        sel = self.group_tree.selection()
        if not sel:
            self.status.config(text="Выберите группу слева.")
            return
        gid = sel[0]
        df = self.app.df
        mask = self._masks.get(gid)
        rows = df[mask] if mask is not None else df.iloc[0:0]
        shown = rows.head(self._SHOW_LIMIT)
        suffix = f" (показаны первые {self._SHOW_LIMIT})" if len(rows) > self._SHOW_LIMIT else ""
        if gid == self.UNDEFINED_ID:
            # итерация 33: drill-down ВСЕХ колонок каталога, не только POS_COLS
            cols = list(df.columns)
            self._set_pos_columns(cols)
            for _, r in shown.iterrows():
                self.pos_tree.insert("", "end", values=[_fmt_cell(r, c) for c in cols])
            share = len(rows) / len(df) if len(df) else 0.0
            self.status.config(text=f"[{gid}] поставщик не определён: {len(rows)} позиций "
                                    f"({share:.1%} каталога){suffix}")
            return
        self._set_pos_columns(self.POS_COLS)
        g = next(x for x in self.groups if x["id"] == gid)
        for _, r in shown.iterrows():
            self.pos_tree.insert("", "end", values=[_fmt_cell(r, c) for c in self.POS_COLS])
        self.status.config(text=f"[{g['id']}] {g['title']} -> {', '.join(g['suppliers'])}: "
                                f"{len(rows)} позиций{suffix}")


# ============================ ВКЛАДКА 4: ИСТОЧНИКИ ============================
# Портфолио: вместо листов закрытых прайс-листов — входные файлы демо-сборки
# (сид каталога и «грязный» мок-прайс по листам) и служебные таблицы SQLite:
# карантин (строки прайса, которые нельзя однозначно превратить в позицию) и
# журнал изменений цен при слиянии прайса с каталогом.
SOURCE_COLS = ["SKU", "Категория", "Тип_изделия", "Стандарт", "Маркировка_в_прайсе",
               "DN / Размер", "PN / Давление", "Материал", "Толщина / Высота",
               "Цена_без_НДС", "Остаток", "Поставщик", "Координата"]
SEED_FILE = "mock_catalog_seed.json"
PRICES_FILE = "mock_prices.xlsx"


class SourcesTab(ttk.Frame):
    QUAR_COLS = ["Лист", "Строка", "Причина", "Исходные значения"]
    UPD_COLS = ["SKU", "Было", "Стало", "Δ, %", "Поставщик"]

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app

        # status — первым (side="bottom"), иначе expand-панели заберут место
        self.status = ttk.Label(self, text="")
        self.status.pack(side="bottom", fill="x", padx=6, pady=4)

        left = ttk.Frame(self)
        left.pack(side="left", fill="y", padx=6, pady=6)
        ttk.Label(left, text="Входные данные сборки:").pack(anchor="w")
        self.tree = ttk.Treeview(left, show="tree", height=30)
        self.tree.column("#0", width=300)
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        right = ttk.Panedwindow(self, orient="vertical")
        right.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.right_pane = right
        top = ttk.LabelFrame(right, text="Позиции каталога из выбранного источника")
        right.add(top, weight=3)
        self.result_tree = self._make_table(top, SOURCE_COLS, 110)
        make_copy_menu(self.result_tree, "Маркировка_в_прайсе")

        bottom = ttk.Notebook(right)
        right.add(bottom, weight=2)
        self.quarantine = db_sqlite.load_table("quarantine", app.db_path)
        self.updates = db_sqlite.load_table("price_updates", app.db_path)
        qf = ttk.Frame(bottom)
        bottom.add(qf, text=f"Карантин прайса ({len(self.quarantine)})")
        self.quar_tree = self._make_table(qf, self.QUAR_COLS, 160)
        self.quar_tree.column("Исходные значения", width=600)
        make_copy_menu(self.quar_tree, "Причина")
        uf = ttk.Frame(bottom)
        bottom.add(uf, text=f"Изменения цен ({len(self.updates)})")
        self.upd_tree = self._make_table(uf, self.UPD_COLS, 130)
        self.upd_tree.column("SKU", width=300)
        make_copy_menu(self.upd_tree, "SKU")
        self.bottom = bottom

        self._fill_service_tables()
        self._build_tree()

    @staticmethod
    def _make_table(parent, cols, width):
        tree = ttk.Treeview(parent, columns=cols, show="headings", height=8)
        for c in cols:
            tree.heading(c, text=c)
            tree.column(c, width=width, anchor="w")
        vsb = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        _add_hscroll(tree)
        _bind_mousewheel(tree, tree)
        return tree

    def _fill_service_tables(self):
        q = self.quarantine
        for _, r in q.iterrows():
            self.quar_tree.insert("", "end", values=[_fmt(r.get("sheet")), _fmt(r.get("row")),
                                                     _fmt(r.get("reason")), _fmt(r.get("raw"))])
        if not len(q):
            self.quar_tree.insert("", "end", values=["", "", "карантин пуст или БД не собрана", ""])
        u = self.updates
        for _, r in u.iterrows():
            old, new = r.get("old_price"), r.get("new_price")
            delta = (f"{100.0 * (new - old) / old:+.1f}"
                     if pd.notna(old) and pd.notna(new) and old else "")
            self.upd_tree.insert("", "end", values=[_fmt(r.get("sku")), _fmt(old), _fmt(new),
                                                    delta, _fmt(r.get("vendor"))])

    def _build_tree(self):
        df = self.app.df
        src = df["Источник"].fillna("").astype(str)
        coord = df["Координата"].fillna("").astype(str)
        sheet_of = coord.str.split("!", n=1).str[0]
        self.tree.delete(*self.tree.get_children())

        n_seed = int((src == "SEED").sum())
        self.tree.insert("", "end", iid="seed", values=("SEED", ""),
                         text=f"{SEED_FILE}  · {n_seed} поз.")

        path = core.DATA_DIR / PRICES_FILE
        try:
            sheets = pd.ExcelFile(path).sheet_names
        except Exception as e:
            sheets = []
            self.status.config(text=f"Не удалось открыть {path.name}: {e}")
        root_id = self.tree.insert("", "end", iid="prices", text=PRICES_FILE, open=True)
        q_by_sheet = (self.quarantine["sheet"].value_counts().to_dict()
                      if len(self.quarantine) else {})
        for sheet in sheets:
            if sheet.lower().startswith("readme"):   # как build_mock.load_price_list
                self.tree.insert(root_id, "end", text=f"{sheet}  · служебный (игнор)")
                continue
            n = int(((src == "PRICE") & (sheet_of == sheet)).sum())
            label = f"{sheet}  · {n} новых поз."
            if q_by_sheet.get(sheet):
                label += f", карантин {q_by_sheet[sheet]}"
            self.tree.insert(root_id, "end", text=label, values=("PRICE", sheet))

        n_data = sum(1 for s in sheets if not s.lower().startswith("readme"))
        meta = freshness.read_meta(self.app.db_path)
        date_txt = freshness.format_ts(meta.get("ts")) if meta else "каталог не собран"
        if sheets or not self.status.cget("text"):
            self.status.config(
                text=f"сид: {n_seed} SKU / листов прайса: {n_data} / новых SKU из прайса: "
                     f"{int((src == 'PRICE').sum())} / изменений цен: {len(self.updates)} / "
                     f"в карантине: {len(self.quarantine)} / сборка: {date_txt}. "
                     f"Обновлённые цены остаются у строк сида (журнал — вкладка «Изменения цен»).")

    def _on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0]).get("values")
        if not vals:
            return  # корень файла прайса, а не лист
        src_code, sheet = str(vals[0]), str(vals[1]) if len(vals) > 1 else ""
        df = self.app.df
        mask = df["Источник"].fillna("").astype(str) == src_code
        if sheet:
            mask &= df["Координата"].fillna("").astype(str).str.startswith(f"{sheet}!")
        self.result_tree.delete(*self.result_tree.get_children())
        for _, row in df[mask].iterrows():
            self.result_tree.insert("", "end", values=[_fmt_cell(row, c) for c in SOURCE_COLS])


# ============================ ГЛАВНОЕ ОКНО ============================

class App:
    def __init__(self, root, db_path=None):
        self.root = root
        self.root.title("Industrial Sealing Materials Matcher — демо (синтетические данные)")
        self.root.geometry("1280x800")
        self.db_path = db_sqlite.resolve_path(db_path)
        # высота строки таблиц — от метрики шрифта: на экранах с масштабом
        # 125–175% фиксированная высота строки ttk.Treeview обрезает текст
        line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        ttk.Style(root).configure("Treeview", rowheight=line + 6)

        try:
            self.df = db_sqlite.load_catalog(self.db_path)
        except Exception as e:
            messagebox.showerror("Каталог", f"Не удалось загрузить каталог: {e}")
            self.df = pd.DataFrame(columns=core.COLS_RU)

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True)

        self.order_tab = OrderTab(self.notebook, self)
        self.catalog_tab = CatalogTab(self.notebook, self)
        self.suppliers_tab = SuppliersTab(self.notebook, self)
        self.sources_tab = SourcesTab(self.notebook, self)

        self.notebook.add(self.order_tab, text="Подбор заявок")
        self.notebook.add(self.catalog_tab, text="Каталог")
        self.notebook.add(self.suppliers_tab, text="Поставщики")
        self.notebook.add(self.sources_tab, text="Источники")

        _bind_clipboard_keys(self.root)


def main(db_path=None):
    root = tk.Tk()
    App(root, db_path)
    root.mainloop()


if __name__ == "__main__":
    main()
