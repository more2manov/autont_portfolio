# -*- coding: utf-8 -*-
"""
src/parsers.py — парсеры прайсов (итерация 2, SOLID).

  * ColumnMap          — типизированная карта колонок СНП-листа;
  * build_colmap       — координатор из небольших подфункций (S);
  * parse_snp_df       — СНП ГОСТ/ОСТ по заголовкам;
  * parse_asme_snp_df  — СНП ASME B16.20;
  * parse_ring_table   — кольца овальные / восьмиугольные / БХ;
  * parse_packing      — набивки (все ценовые схемы + листы без цен);
  * parse_simple       — простые листы (ленты);
  * parse_extractors   — экстракторы (блок Vendor T).

Все парсеры принимают TableData (D); маркировки разбирает MarkParser (O).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import core
from mark_parser import get_mark_parser
from table_data import TableData, as_table

# Реальный минимум цены СНП в прайсах ~28 руб.; всё ниже — артефакт парсинга.
MIN_SNP_PRICE = 5.0


# ============================ КАРТА КОЛОНОК ============================

@dataclass
class ColumnMap:
    """Карта колонок СНП-листа.

    Именованные поля вместо словаря: типизация, автодополнение, методы.
    """
    mark: int
    pressure: int | None
    dims: dict
    h: int | None
    ring: int | None
    weight_cols: list
    price_cols: list
    th_values: list
    unit: str | None
    data_start: int

    def __getitem__(self, key):
        """Совместимость со словарным доступом (тесты итерации 1)."""
        return getattr(self, key)

    def get_price_cols(self) -> list:
        return self.price_cols

    def get_weight(self, table: TableData, row_idx: int,
                   thickness: float | None) -> float | None:
        """Вес для строки данных: один столбец — общий; несколько — по толщине."""
        if not self.weight_cols:
            return None
        if len(self.weight_cols) == 1:
            return core.clean_num(table.get_cell(row_idx, self.weight_cols[0]))
        if thickness in self.th_values:
            k = self.th_values.index(thickness)
            if k < len(self.weight_cols):
                return core.clean_num(table.get_cell(row_idx, self.weight_cols[k]))
        return None


_BLOCK_ROLES = {"mark", "pressure", "d1", "d2", "d3", "d4", "ring", "weight"}
_DIM_RE = re.compile(r"D\s*([1-4])(?!\d)", re.IGNORECASE)


def classify_cell(v):
    """Классифицирует ячейку заголовка -> (роль, данные) или None."""
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    key = core._nc(s)
    if key in core.MAT_NORM:                      # материал: точное совпадение
        return ("mat", core.MAT_NORM[key])
    t = s.upper().replace(" ", "").replace("ММ", "").replace(",", ".")
    if t in ("3.2", "4.5", "6.4"):                # толщина
        return ("th", float(t))
    up = s.upper()
    if "ОБОЗНАЧЕНИЕ" in up or "МАРКИРОВКА" in up:
        return ("mark", None)
    if ("ДАВЛЕНИЕ" in up or up.startswith("РУ,") or up.startswith("РУ ")
            or up == "РУ" or re.search(r"\bPN\b", up)):
        return ("pressure", None)
    m = _DIM_RE.search(up)
    if m:                                         # d1..d4 (проверяем ДО ring!)
        return ("d" + m.group(1), None)
    if "ТОЛЩИНА" in up and "ОГРАНИЧИТ" in up:
        return ("ring", None)
    if "ВЫСОТА" in up:
        return ("h", None)
    if "ТОЛЩИНА" in up and re.search(r"(?<![A-ZА-Я])H(?![A-ZА-Я])", up):
        return ("h", None)
    if up.startswith("ВЕС"):
        return ("weight", None)
    if "СТОИМОСТЬ" in up or "ЦЕНА" in up:
        return ("group", up)
    return None


# ---------- подфункции build_colmap (каждая — одна ответственность) ----------

def _header_rows(mark_row: int, data_start: int) -> list:
    """Заголовочные строки: от якоря маркировки до данных, не более 12."""
    return list(range(mark_row, data_start))[-12:]


def find_mark_anchor(table: TableData, max_scan: int = 40):
    """Ищет колонку/строку «Обозначение|Маркировка»."""
    nrows, ncols = table.get_shape()
    for i in range(min(nrows, max_scan)):
        for j in range(ncols):
            cls = classify_cell(table.get_cell(i, j))
            if cls and cls[0] == "mark":
                return j, i
    return None, None


def find_data_start(table: TableData, mark_col: int, mark_row: int) -> int | None:
    """Первая строка данных: маркировка в mark_col распознаётся MarkParser-ом."""
    mp = get_mark_parser()
    nrows, _ = table.get_shape()
    for i in range(mark_row, nrows):
        if mp.parse(table.get_cell(i, mark_col)):
            return i
    return None


def classify_headers(table: TableData, mark_row: int, data_start: int):
    """Разбор заголовочных строк: роли колонок + материалы/толщины/группы.

    Группа и материал заполняются вправо по строке (объединённые ячейки
    дают значение только в первой колонке диапазона). Толщины переносом
    НЕ заполняются: 3.2 и 4.5 всегда стоят в своих колонках явно.
    """
    _, ncols = table.get_shape()
    role, mat_of, th_of, group_of = {}, {}, {}, {}
    for i in _header_rows(mark_row, data_start):
        last_g = last_m = None
        for j in range(ncols):
            cls = classify_cell(table.get_cell(i, j))
            kind = cls[0] if cls else None
            if kind == "group":
                last_g = cls[1]
            elif cls is not None and kind not in ("mat", "th"):
                last_g = None
            if kind == "mat":
                last_m = cls[1]
            elif cls is not None and kind not in ("group", "th"):
                last_m = None
            if last_g:
                group_of.setdefault(j, last_g)
            if last_m:
                mat_of.setdefault(j, last_m)
            if kind == "th":
                th_of.setdefault(j, cls[1])
            if cls is not None and kind not in ("group", "mat", "th"):
                role.setdefault(j, kind)
    # контекстные колонки (материал/толщина) не могут быть служебными
    for j in [j for j in mat_of if role.get(j) in _BLOCK_ROLES]:
        del mat_of[j]
    for j in [j for j in th_of if role.get(j) in _BLOCK_ROLES]:
        del th_of[j]
    return role, mat_of, th_of, group_of


def _synthesize_materials(ctx_cols, mat_of, th_of, table, mark_col, data_start):
    """Строки материалов нет — синтезируем по списку семейства (как монолит)."""
    info = get_mark_parser().parse(table.get_cell(data_start, mark_col))
    fam = core.FAMILIES.get((info["standard"], info["type"])) if info else None
    if not fam:
        return
    step = max(1, len(set(th_of.values())))
    for idx, j in enumerate(ctx_cols):
        mat_of[j] = fam["mats"][(idx // step) % len(fam["mats"])]


def build_price_cols(role, mat_of, th_of, group_of,
                     table=None, mark_col=None, data_start=None) -> list:
    """Ценовые колонки = колонки с контекстом материал/толщина."""
    ctx_cols = sorted(set(mat_of) | set(th_of))
    price_cols = []
    for j in ctx_cols:
        if role.get(j) in _BLOCK_ROLES:
            continue
        price_cols.append({
            "col": j,
            "material": mat_of.get(j),
            "thickness": th_of.get(j),
            "fullmetal": "НАВИТАЯ" in (group_of.get(j) or ""),
        })
    return price_cols


def build_weight_cols(table, role, price_cols, mat_of, th_of, data_start) -> list:
    """Вес: роли «weight» + достройка вправо по числу толщин
    (объединённый «ВЕС (кг)» даёт роль только первой колонке)."""
    _, ncols = table.get_shape()
    w_first = min((j for j, r in role.items() if r == "weight"), default=None)
    if w_first is None:
        return []
    th_values = sorted({pc["thickness"] for pc in price_cols
                        if pc["thickness"] is not None})
    if th_values:
        return [w_first + k for k in range(len(th_values)) if w_first + k < ncols]
    return [w_first]


def detect_unit(table, mark_row, data_start) -> str | None:
    _, ncols = table.get_shape()
    found_kgs = found_mpa = False
    for i in _header_rows(mark_row, data_start):
        for j in range(ncols):
            up = table.get_cell(i, j).upper()
            if not up:
                continue
            if "КГ" in up and "/СМ" in up:
                found_kgs = True
            if "МПА" in up:
                found_mpa = True
    return "kgs" if found_kgs else ("mpa" if found_mpa else None)


def find_pressure_col(role) -> int | None:
    return next((j for j, r in role.items() if r == "pressure"), None)


def find_ring_col(role) -> int | None:
    return next((j for j, r in role.items() if r == "ring"), None)


def find_h_col(role, mat_of, th_of) -> int | None:
    """Высота как колонка данных — только если у неё нет ценового контекста."""
    return next((j for j, r in role.items()
                 if r == "h" and j not in mat_of and j not in th_of), None)


def build_colmap(src, max_scan: int = 40) -> ColumnMap | None:
    """Собирает карту колонок СНП-листа (координатор подфункций)."""
    table = as_table(src)
    mark_col, mark_row = find_mark_anchor(table, max_scan)
    if mark_col is None:
        return None
    data_start = find_data_start(table, mark_col, mark_row)
    if data_start is None:
        return None
    role, mat_of, th_of, group_of = classify_headers(table, mark_row, data_start)
    # ОСТ: групповой заголовок «Толщина h, (мм)» висит НАД ценовыми парами —
    # такая роль h не является колонкой данных, если у колонки есть mat/th-контекст
    for j in [j for j, r in list(role.items())
              if r == "h" and (j in mat_of or j in th_of)]:
        del role[j]
    # материал/толщина не могут быть правее начала зоны веса
    w_first = min((j for j, r in role.items() if r == "weight"), default=None)
    if w_first is not None:
        mat_of = {j: m for j, m in mat_of.items() if j < w_first}
        th_of = {j: t for j, t in th_of.items() if j < w_first}
    # Лист без строки материалов — не ценовая матрица СНП:
    # legacy-листы («Наличие на складе») иначе дают мусор через синтез марок
    if not mat_of:
        return None
    price_cols = build_price_cols(role, mat_of, th_of, group_of,
                                  table, mark_col, data_start)
    if not price_cols or not any(pc["material"] for pc in price_cols):
        return None
    weight_cols = build_weight_cols(table, role, price_cols, mat_of, th_of, data_start)
    return ColumnMap(
        mark=mark_col,
        pressure=find_pressure_col(role),
        dims={r: j for j, r in role.items() if r in ("d1", "d2", "d3", "d4")},
        h=find_h_col(role, mat_of, th_of),
        ring=find_ring_col(role),
        weight_cols=weight_cols,
        price_cols=price_cols,
        th_values=sorted({pc["thickness"] for pc in price_cols
                          if pc["thickness"] is not None}),
        unit=detect_unit(table, mark_row, data_start),
        data_start=data_start,
    )


def describe_colmap(name: str, cm) -> str:
    """Человекочитаемое описание карты колонок (для --debug)."""
    if cm is None:
        return f"«{name}»: colmap НЕ построен"
    lines = [f"«{name}»: mark={cm['mark']} press={cm['pressure']} "
             f"dims={cm['dims']} h={cm['h']} ring={cm['ring']} "
             f"weights={cm['weight_cols']} th={cm['th_values']} unit={cm['unit']} "
             f"data_start={cm['data_start']}"]
    for pc in cm["price_cols"]:
        grp = "НАВИТАЯ" if pc["fullmetal"] else "-"
        lines.append(f"   col {pc['col']:>2}: mat={pc['material']!r:<10} "
                     f"th={pc['thickness']} group={grp}")
    return "\n".join(lines)


# ============================ СНП ГОСТ / ОСТ ============================

def parse_snp_df(src) -> list:
    """Разбирает лист СНП по карте колонок; каждая ценовая ячейка -> позиция."""
    table = as_table(src)
    cm = build_colmap(table)
    if cm is None:
        return []
    mp = get_mark_parser()
    nrows, _ = table.get_shape()
    rows = []
    for i in range(cm.data_start, nrows):
        mark = table.get_cell(i, cm.mark)
        info = mp.parse(mark)
        if not info or (info["standard"], info["type"]) not in core.FAMILIES:
            continue

        pressure = core.norm_pressure(
            table.get_cell(i, cm.pressure) if cm.pressure is not None else "")
        unit = cm.unit or ("kgs" if info["standard"] == "ГОСТ" else "mpa")
        pvals = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", pressure)]
        if pvals:
            lo, hi = min(pvals), max(pvals)
            if unit == "kgs":
                mlo, mhi, klo, khi = lo / 10.0, hi / 10.0, lo, hi
            else:
                mlo, mhi, klo, khi = lo, hi, lo * 10.0, hi * 10.0
        else:
            mlo = mhi = klo = khi = None

        dims = {}
        for name, j in sorted(cm.dims.items()):
            v = core.clean_num(table.get_cell(i, j))
            if v is not None:
                dims[name] = v
        h_val = core.clean_num(table.get_cell(i, cm.h)) if cm.h is not None else None
        ring_val = (core.clean_num(table.get_cell(i, cm.ring))
                    if cm.ring is not None else None)

        for pc in cm.price_cols:
            if pc["material"] is None:
                continue
            price = core.clean_num(table.get_cell(i, pc["col"]))
            if price is None or price < MIN_SNP_PRICE:
                continue
            thickness = pc["thickness"] if pc["thickness"] is not None else h_val
            weight = cm.get_weight(table, i, pc["thickness"])

            mn, mc = core.mat_norm(pc["material"]), core.mat_code(pc["material"])
            mark_out = mark + (" " + core.GROUP_FULLMETAL if pc["fullmetal"] else "")
            if info["standard"] == "ГОСТ":
                sku = f"SNP-{info['type']}-{info['execution']}-ГОСТ-DN{info['dn']}-{pressure}-{mc}"
            else:
                sku = f"SNP-{info['type']}-ОСТ-SIZE{info['dn']}-{pressure}-{mc}"
            if thickness not in (None, ""):
                sku += f"-H{thickness}"
            if dims:
                sku += "-D" + "-".join(
                    f"{k}{v:g}" for k, v in sorted(dims.items()) if v is not None)
            if pc["fullmetal"]:
                sku += "-" + core.GROUP_FULLMETAL

            dims_txt = ", ".join(f"{k}={v}" for k, v in sorted(dims.items()))
            if ring_val is not None:
                dims_txt += (", " if dims_txt else "") + f"ring={ring_val}"

            rows.append(dict(
                SKU=sku, Категория="СНП", Тип_изделия=info["type"],
                Стандарт=info["standard"], Маркировка_в_прайсе=mark_out,
                **{"DN / Размер": info["dn"], "PN / Давление": pressure},
                Материал=mn, **{"Толщина / Высота": thickness},
                Основные_размеры=dims_txt, Вес_кг=weight, Цена_без_НДС=price,
                Исполнение=info["execution"], Наполнитель=info["filler"],
                Давление_МПа_мин=mlo, Давление_МПа_макс=mhi,
                Давление_кгс_мин=klo, Давление_кгс_макс=khi,
                Источник=core.CUR["src"],
                Координата=f'{core.CUR["sheet"]}!{core.xl_cell(i, cm.mark)}',
            ))
    return rows


# ============================ СНП ASME B16.20 ============================

def _nearest_above(items, i):
    best = None
    for r, v in items:
        if r < i:
            best = (r, v)
        else:
            break
    return best


def _find_mat_header_rows(table):
    out = []
    nrows, ncols = table.get_shape()
    for i in range(nrows):
        mapping = {}
        for j in range(ncols):
            n = core._nc(table.get_cell(i, j))
            if n in core.MAT_NORM:
                mapping[j] = core.MAT_NORM[n]
        if len(mapping) >= 3:
            out.append((i, mapping))
    return out


def _find_th_header_rows(table):
    out = []
    nrows, ncols = table.get_shape()
    for i in range(nrows):
        vals = []
        for j in range(ncols):
            s = core._nc(table.get_cell(i, j)).replace("ММ", "").replace("MM", "")
            if s in ("3.2", "4.5", "6.4", "3,2", "4,5", "6,4"):
                vals.append(float(s.replace(",", ".")))
        if len(vals) >= 2:
            out.append((i, sorted(set(vals))))
    return out


_ASME_TYPE = ("А", "Б", "В", "Г", "Д")
_ASME_FLANGE = re.compile(r"RF|FF|RJ|RTJ|STG|LTG|LMF|SMF|TG|MF|GF")
_ASME_SIZE = re.compile(r'^\d+(?:\s\d+/\d+)?(?:/\d+)?["”]$')


def parse_asme_snp_df(src) -> list:
    table = as_table(src)
    nrows, ncols = table.get_shape()
    if ncols < 5:                                 # узкий лист — не таблица ASME
        return []
    mat_rows = _find_mat_header_rows(table)
    th_rows = _find_th_header_rows(table)
    rows = []
    for i in range(nrows):
        c0 = table.get_cell(i, 0).upper()
        c1 = table.get_cell(i, 1).upper()
        c2 = table.get_cell(i, 2)
        if c0 not in _ASME_TYPE or not _ASME_FLANGE.search(c1):
            continue
        if not _ASME_SIZE.match(c2):
            continue
        cls_nums = [int(x) for x in re.findall(r"\d+", table.get_cell(i, 3))]
        if not cls_nums:
            continue
        cls_lo, cls_hi = cls_nums[0], cls_nums[-1]

        nums = []
        for j in range(4, ncols):
            v = core.clean_num(table.get_cell(i, j))
            if v is not None:
                nums.append(v)
        if len(nums) < 19:                        # >=1 размер + 16 цен + 2 веса
            continue
        weights, prices = nums[-2:], nums[-18:-2]
        dims = nums[:-18][:4]

        mh = _nearest_above(mat_rows, i)
        mats = [mh[1][c] for c in sorted(mh[1])] if mh and len(mh[1]) == 8 else core.M8
        th = _nearest_above(th_rows, i)
        ths = th[1] if th and len(th[1]) == 2 else [4.5, 6.4]
        dim_names = ["d1", "d2", "d3", "d4"] if len(dims) == 4 else ["d2", "d3", "d4"]
        dims_d = dict(zip(dim_names, dims))

        size_label = c2.replace("\u201c", '"').replace("\u201d", '"')
        pnorm = f"CL{cls_lo}" if cls_lo == cls_hi else f"CL{cls_lo}-{cls_hi}"
        for m_idx, mat in enumerate(mats):
            for t_idx, t in enumerate(ths):
                p_idx = m_idx * 2 + t_idx
                if p_idx >= len(prices):
                    continue
                price = prices[p_idx]
                if price is None or price <= 0:
                    continue
                mn, mc = core.mat_norm(mat), core.mat_code(mat)
                sku = (f"SNP-{c0}-ASME-{c1}-{size_label.replace('/', '-')[:-1]}in-"
                       f"{pnorm}-{mc}-H{t}")
                rows.append(dict(
                    SKU=sku, Категория="СНП", Тип_изделия=c0, Стандарт="ASME",
                    Маркировка_в_прайсе=f"СНП {c0} {c1} {size_label} {pnorm}",
                    **{"DN / Размер": size_label, "PN / Давление": pnorm},
                    Материал=mn, **{"Толщина / Высота": t},
                    Основные_размеры=", ".join(f"{k}={v}" for k, v in dims_d.items()),
                    Вес_кг=weights[t_idx] if t_idx < len(weights) else None,
                    Цена_без_НДС=price,
                    Исполнение=None, Наполнитель=None,
                    Давление_МПа_мин=None, Давление_МПа_макс=None,
                    Давление_кгс_мин=None, Давление_кгс_макс=None,
                    Источник=core.CUR["src"],
                    Координата=f'{core.CUR["sheet"]}!A{i + 1}',
                ))
    return rows


# ============================ КОЛЬЦА (овал / 8-угол / БХ) ============================

def find_header_and_data(table, data_pattern, max_back=15):
    """Строка заголовка (ближайшая сверху к данным с «Маркировка/Обозначение»)
    и первая строка данных."""
    nrows, ncols = table.get_shape()
    for i in range(nrows):
        val = table.get_cell(i, 0)
        if not val:
            continue
        if re.match(data_pattern, val, re.IGNORECASE):
            for j in range(i - 1, max(i - max_back - 1, -1), -1):
                if any(("маркировка" in table.get_cell(j, c).lower()
                        or "обозначение" in table.get_cell(j, c).lower())
                       for c in range(ncols)):
                    return j, i
            return None, i
    return None, None


def _material_cols(table, data_start):
    """Колонки материалов для листов колец.

    ВАЖНО: над данными колец лежит НЕСКОЛЬКО строк-синонимов материалов
    (русские марки / латинские классы). Берём строку с МАКСИМАЛЬНЫМ числом
    распознанных материалов: ближайшая к данным строка описывает только
    6 из 8 колонок (08Х13 и 08Х18Н10 терялись в первой версии).
    """
    _, ncols = table.get_shape()
    best_cols, best_hits = [], -1
    for r in range(max(0, data_start - 10), data_start):
        cols = []
        for c in range(ncols):
            h = table.get_cell(r, c)
            if h in core.MATERIAL_MAP:
                cols.append((c, h))
        if len(cols) > best_hits:
            best_hits, best_cols = len(cols), cols
    return best_cols


def parse_ring_table(src, category, type_label, standard, sku_prefix, data_pattern):
    table = as_table(src)
    hr, ds = find_header_and_data(table, data_pattern)
    if hr is None:
        return []
    nrows, ncols = table.get_shape()
    header = [table.get_cell(hr, j) for j in range(ncols)]
    low = [h.lower() for h in header]
    mark_col = next((i for i, h in enumerate(low) if "маркировка" in h), 0)
    dn_col = next((i for i, h in enumerate(low) if h in ("ду", "dn")), None)
    press_col = next((i for i, h in enumerate(low)
                      if "(мпа)" in h or "ру" in h), None)
    weight_col = next((i for i, h in enumerate(low) if "вес" in h), None)

    mat_cols = _material_cols(table, ds)
    if not mat_cols:
        return []
    first_mat = min(i for i, _ in mat_cols)
    start = press_col + 1 if press_col is not None else mark_col + 1
    dims = {header[i]: i for i in range(start, first_mat) if i != dn_col}
    th_key = (next((k for k in dims if re.search("h", k.lower())), None)
              or next((k for k in dims if re.search("s", k.lower())), None))

    rows = []
    for i in range(ds, nrows):
        mark = table.get_cell(i, mark_col)
        if not mark or not re.match(data_pattern, mark, re.IGNORECASE):
            continue
        dn = table.get_cell(i, dn_col) if dn_col is not None else ""
        pn = core.norm_pressure(table.get_cell(i, press_col)) if press_col is not None else ""
        dv = {}
        for k, idx in dims.items():
            raw = table.get_cell(i, idx)
            v = core.clean_num(raw)
            dv[k] = v if v is not None else raw
        th = dv.get(th_key) if th_key else None
        wt = core.clean_num(table.get_cell(i, weight_col)) if weight_col is not None else None

        for col, mat in mat_cols:
            price = core.clean_num(table.get_cell(i, col))
            if price is None or price <= 0:
                continue
            mn, mc = core.mat_norm(mat), core.mat_code(mat)
            dn_label = f"DN{dn}" if dn else ""
            sku = "-".join(x for x in [sku_prefix, standard, dn_label, pn, mc] if x)
            if th not in (None, ""):
                sku += f"-H{th}"
            rows.append(dict(
                SKU=sku, Категория=category, Тип_изделия=type_label, Стандарт=standard,
                Маркировка_в_прайсе=mark,
                **{"DN / Размер": dn, "PN / Давление": pn}, Материал=mn,
                **{"Толщина / Высота": th},
                Основные_размеры=", ".join(f"{k}={v}" for k, v in dv.items()
                                           if v not in ("", None)),
                Вес_кг=wt, Цена_без_НДС=price,
                Исполнение=None, Наполнитель=None,
                Источник=core.CUR["src"],
                Координата=f'{core.CUR["sheet"]}!{core.xl_cell(i, mark_col)}',
            ))
    return rows

# ============================ НАБИВКИ ============================

def parse_packing(src):
    """Набивки: все ценовые таблицы листа (левые+правые), включая позиции без цены."""
    table = as_table(src)
    nrows, ncols = table.get_shape()
    rows = []
    anchors = []
    for i in range(nrows):
        cells = [table.get_cell(i, j) for j in range(ncols)]
        low = [c.lower() for c in cells]
        joined = "|".join(low)
        if ("цена розн" in joined or "цена при заказе" in joined
                or "вход" in low or "продажа" in low):
            cand = []
            for j, h in enumerate(low):
                if "цена розн" in h or "цена при заказе 2" in h:
                    cand.append((j - 3, j, 0))
                elif h == "продажа":
                    cand.append((j - 4, j, 0))    # имя|ед|кол-во|Вход|Продажа
                elif h == "вход":
                    cand.append((j - 3, j, 1))    # 1 = входная (не продажная)
            cand.sort(key=lambda t: (t[0], t[2]))
            covered = {nc for nc, _, _ in cand if nc >= 0}
            merged = [a for a in anchors if a[0] not in covered]
            seen = set()
            for nc, pc, kind in cand:
                if nc in seen or nc < 0:
                    continue
                seen.add(nc)
                # колонка кол-ва якоря: по заголовку («кол-во»/«количество»)
                # в диапазоне [nc, pc); без заголовка — позиционно по схеме
                # якоря (5-колоночная «имя|ед|кол-во|Вход|Продажа»: pc-nc==4
                # -> кол-во=pc-2; 4-колоночные «цена розн»/«вход»: pc-nc==3
                # -> кол-во=pc-1).
                qc = next((c for c in range(nc, pc)
                           if low[c] in ("кол-во", "количество")), None)
                if qc is None:
                    qc = pc - 2 if pc - nc == 4 else pc - 1
                merged.append((nc, pc, kind, qc))
            anchors = merged
            continue
        if not anchors:
            continue
        for name_col, price_col, kind, qty_col in anchors:
            if price_col >= ncols:
                continue
            name = cells[name_col]
            if not name or len(name) < 3 or not re.search(r"[A-Za-zА-Яа-я]", name):
                continue
            if re.search(r"(цена|наименование|ед\.?изм|столбец|итого|всего|в кат|по \d)",
                         name, re.I):
                continue
            price = core.clean_num(cells[price_col])
            if price is not None and price == 0:
                price = None                      # позиция есть, цена неизвестна
            if kind == 1 or (price is not None and price > 30000):
                price = None                      # входная или цена за бухту
            stock = None
            if 0 <= qty_col < ncols:
                sv = core.clean_num(cells[qty_col])
                if sv is not None and sv >= 0:
                    stock = int(sv)
            rows.append(dict(
                SKU=f"PACKING-{re.sub(r'[^A-Za-z0-9]', '', core.norm_text(name))}",
                Категория="Набивки", Тип_изделия="Набивка", Стандарт="",
                Маркировка_в_прайсе=name,
                **{"DN / Размер": "", "PN / Давление": ""},
                Материал="", **{"Толщина / Высота": None},
                Основные_размеры="", Вес_кг=None, Остаток=stock,
                Цена_без_НДС=price,
                Исполнение=None, Наполнитель=None,
                Источник=core.CUR["src"],
                Координата=f'{core.CUR["sheet"]}!{core.xl_cell(i, name_col)}'))
    if not rows:                                  # листы без цен (фторопласт/графит)
        header = []
        for i in range(nrows):
            cells = [table.get_cell(i, j) for j in range(ncols)]
            low = [c.lower() for c in cells]
            if any(h in ("наименование", "размер", "столбец 1") for h in low):
                header = low
                continue
            if not header:
                continue
            for j, h in enumerate(header):
                if h not in ("наименование", "размер", "столбец 1") or j >= ncols:
                    continue
                name = cells[j]
                if (len(name) < 3 or not re.search(r"\d", name)
                        or not re.search(r"[A-Za-zА-Я]", name)):
                    continue
                mat = cells[j + 1] if j + 1 < ncols and header[j + 1] == "материал" else ""
                rows.append(dict(
                    SKU=f"PACKING-{re.sub(r'[^A-Za-z0-9]', '', core.norm_text(name))}",
                    Категория="Набивки", Тип_изделия="Набивка", Стандарт="",
                    Маркировка_в_прайсе=name,
                    **{"DN / Размер": "", "PN / Давление": ""},
                    Материал=mat, **{"Толщина / Высота": None},
                    Основные_размеры="", Вес_кг=None,
                    Цена_без_НДС=None,
                    Исполнение=None, Наполнитель=None,
                    Источник=core.CUR["src"],
                    Координата=f'{core.CUR["sheet"]}!{core.xl_cell(i, j)}'))
    return rows


# ============================ ПРОСТЫЕ ЛИСТЫ (ЛЕНТЫ) ============================

ME502_SHEET = "МЕ502 и ТМГ-0142Ф"

def parse_simple(src, header_keys, name_key, price_key, category, type_label, sku_prefix):
    table = as_table(src)
    nrows, ncols = table.get_shape()
    hr = None
    for i in range(nrows):
        vals = [table.get_cell(i, j).lower() for j in range(ncols)]
        vals = [v for v in vals if v]
        if any(k in x for x in vals for k in header_keys):
            hr = i
            break
    if hr is None:
        return []
    header = [table.get_cell(hr, j).lower() for j in range(ncols)]
    nc = next((i for i, h in enumerate(header) if name_key in h), None)
    pc = next((i for i, h in enumerate(header) if price_key in h), None)
    if nc is None or pc is None:
        return []
    # итерация 44: у сайд-таблицы K–Q листа «МЕ502 и ТМГ-0142Ф» собственных
    # цен нет (O/P/Q пусты); pc указывал на G ОСНОВНОГО блока — цена бралась
    # от чужой строки. Цена NULL (как у BRAND_EQUIV_MD, итерация 31), остаток —
    # «кол-во» самой сайд-таблицы (первая такая колонка правее nc).
    side = core.CUR.get("sheet", "").strip() == ME502_SHEET
    sc = next((j for j in range(nc + 1, ncols) if header[j] == "кол-во"), None) if side else None
    rows = []
    for i in range(hr + 1, nrows):
        name = table.get_cell(i, nc)
        if side:
            if not name:
                continue
            price = None
            sv = core.clean_num(table.get_cell(i, sc)) if sc is not None else None
            stock = int(sv) if sv is not None and sv >= 0 else None
        else:
            price = core.clean_num(table.get_cell(i, pc))
            stock = None
            if not name or price is None or price <= 0:
                continue
        nm = core.norm_text(name)
        rows.append(dict(
            SKU=f"{sku_prefix}-{re.sub(r'[^A-Za-z0-9]', '', nm).upper()}",
            Категория=category, Тип_изделия=type_label, Стандарт="",
            Маркировка_в_прайсе=name,
            **{"DN / Размер": "", "PN / Давление": ""},
            Материал="", **{"Толщина / Высота": None}, Основные_размеры="",
            Вес_кг=None, Остаток=stock, Цена_без_НДС=price, Исполнение=None, Наполнитель=None,
            Источник=core.CUR["src"],
            Координата=f'{core.CUR["sheet"]}!{core.xl_cell(i, nc)}',
        ))
    return rows


# бизнес-решение 24.09: объём ограничен строго восемью парами «оригинал + К»
# листа «МЕ502 и ТМГ-0142Ф» (основной блок колонки A). Остальные строки листа
# (ТМГ-0142Ф, одиночные МЕ502) НЕ парсятся до отдельного решения.
ME502_PAIR_SIZES = ("10х3х10", "12х4х10", "14х5х5", "16х5х5",
                    "17х6х5", "20х4х5", "20х7х5", "25х8х5")
ME502_ALLOW = frozenset(
    [f"Лента МЕ502 {sz}" for sz in ME502_PAIR_SIZES]
    + [f"Лента МЕ502 {sz} К" for sz in ME502_PAIR_SIZES])


def parse_me502_pairs(src) -> list:
    """Основной блок колонки A листа «МЕ502 и ТМГ-0142Ф»: только 16 маркировок
    из ME502_ALLOW. Цена — колонка G «Цена розн без НДС» (цена продажи для КП;
    E «ВХОД» — закупочная, в каталог не идёт, решение итерации 44), остаток —
    «кол-во» (C). Маркировка дословно (с конечной « К»),
    SKU у К-варианта отличается хвостом K."""
    table = as_table(src)
    nrows, ncols = table.get_shape()
    header = [table.get_cell(0, j).lower() for j in range(ncols)]
    sc = next((j for j, h in enumerate(header) if h in ("кол-во", "количество")), None)
    pc = next((j for j, h in enumerate(header) if "цена розн" in h), None)
    if pc is None:
        return []
    rows = []
    for i in range(1, nrows):
        name = table.get_cell(i, 0)
        if "ТМГ" in name:      # начался блок ТМГ-0142Ф — дальше не парсим
            break
        if re.sub(r"\s+", " ", name).strip() not in ME502_ALLOW:
            continue
        price = core.clean_num(table.get_cell(i, pc))
        if price is None or price <= 0:
            continue
        stock = None
        if sc is not None:
            sv = core.clean_num(table.get_cell(i, sc))
            if sv is not None and sv >= 0:
                stock = int(sv)
        nm = core.norm_text(name)
        rows.append(dict(
            SKU=f"TAPE-{re.sub(r'[^A-Za-z0-9]', '', nm).upper()}",
            Категория="Ленты", Тип_изделия="Лента", Стандарт="",
            Маркировка_в_прайсе=name,
            **{"DN / Размер": "", "PN / Давление": ""},
            Материал="", **{"Толщина / Высота": None}, Основные_размеры="",
            Вес_кг=None, Остаток=stock, Цена_без_НДС=price,
            Исполнение=None, Наполнитель=None,
            Источник=core.CUR["src"],
            Координата=f'{core.CUR["sheet"]}!{core.xl_cell(i, 0)}'))
    return rows


# ============================ ЭКСТРАКТОРЫ ============================

def parse_extractors(src):
    """Лист 'Экстракторы': парсим ТОЛЬКО таблицу 'Vendor T'."""
    table = as_table(src)
    nrows, ncols = table.get_shape()
    kg = None
    for i in range(nrows):
        for j in range(ncols):
            if "VENDOR T" in table.get_cell(i, j).upper():
                kg = i
                break
        if kg is not None:
            break
    if kg is None:
        return []
    hr = None
    for i in range(kg + 1, nrows):
        if any("наименование" in table.get_cell(i, j).lower() for j in range(ncols)):
            hr = i
            break
    if hr is None:
        return []
    header = [table.get_cell(hr, j).lower() for j in range(ncols)]
    nc = next((i for i, h in enumerate(header) if "наименование" in h), None)
    dc = next((i for i, h in enumerate(header) if "шнека" in h), None)
    lc = next((i for i, h in enumerate(header) if "длина" in h), None)
    sc = next((i for i, h in enumerate(header)
               if "набивки" in h or "сечени" in h), None)
    pc = next((i for i, h in enumerate(header) if "цена" in h), None)
    oc = next((i for i, h in enumerate(header) if "остаток" in h), None)
    if nc is None or pc is None:
        return []
    rows = []
    for i in range(hr + 1, nrows):
        name = table.get_cell(i, nc)
        if not name or "ЭКСТРАКТОР" not in name.upper():
            continue
        price = core.clean_num(table.get_cell(i, pc))
        if price is None or price <= 0:
            continue
        dims = {}
        if dc is not None and table.get_cell(i, dc):
            dims["шнека"] = table.get_cell(i, dc)
        if lc is not None and table.get_cell(i, lc):
            dims["длина"] = table.get_cell(i, lc)
        if sc is not None and table.get_cell(i, sc):
            dims["сечение"] = table.get_cell(i, sc)
        stock = core.clean_num(table.get_cell(i, oc)) if oc is not None else None
        stock_val = int(stock) if stock is not None else None
        nm = core.norm_text(name)
        rows.append(dict(
            SKU=f"TOOL-{re.sub(r'[^A-Za-z0-9]', '', nm).upper()}",
            Категория="Инструмент", Тип_изделия="Экстрактор", Стандарт="",
            Маркировка_в_прайсе=name,
            **{"DN / Размер": dims.get("шнека", ""), "PN / Давление": ""},
            Материал="", **{"Толщина / Высота": None},
            Основные_размеры=", ".join(f"{k}={v}" for k, v in dims.items()),
            Вес_кг=None, Остаток=stock_val, Цена_без_НДС=price,
            Исполнение=None, Наполнитель=None,
            Поставщик="Vendor T",
            Источник=core.CUR["src"],
            Координата=f'{core.CUR["sheet"]}!{core.xl_cell(i, nc)}',
        ))
    return rows


def parse_copper(src):
    """Лист 'Медные прокладки': ассортимент без цен (левый блок размеров +
    правый блок наименований). Позиции с ценой None, как у ПТФЭ."""
    table = as_table(src)
    nrows, ncols = table.get_shape()
    rows = []
    seen = set()
    for i in range(nrows):
        for nc in (1, 7):
            if nc >= ncols:
                continue
            name = table.get_cell(i, nc)
            if not name or len(str(name)) < 5:
                continue
            s = str(name).strip()
            if not re.search(r"\d", s) or not re.search(r"[A-Za-zА-Яа-я]", s):
                continue
            low = s.lower()
            if any(w in low for w in ("прокладка медные", "ед.изм", "итого")):
                continue
            if nc == 7:
                material = ("Алюминий" if ("алюм" in low or "ад1" in low)
                            else "Медь")
            else:
                material = "Медь"
            key = core.norm_text(s)
            if key in seen:
                continue
            seen.add(key)
            stock = None
            if nc == 1:                            # левый блок: кол-во в колонке 3
                sv = core.clean_num(table.get_cell(i, 3))
                if sv is not None and sv >= 0:
                    stock = int(sv)
            rows.append(dict(
                SKU=f"COPPER-{re.sub(r'[^A-Za-z0-9]', '', key)}",
                Категория="Прокладки", Тип_изделия="Прокладка",
                Стандарт="", Маркировка_в_прайсе=s,
                **{"DN / Размер": "", "PN / Давление": ""},
                Материал=material, **{"Толщина / Высота": None},
                Основные_размеры="", Вес_кг=None, Остаток=stock, Цена_без_НДС=None,
                Исполнение=None, Наполнитель=None,
                Источник=core.CUR["src"],
                Координата=f'{core.CUR["sheet"]}!{core.xl_cell(i, nc)}'))
    return rows


def parse_stock_sheet(src, category: str) -> list:
    """Листы-склады (напр. «Прокладки ПТФЭ»): параллельные группы
    имя|Материал|Толщина|кол-во. Цен нет -> позиции с ценой None."""
    table = as_table(src)
    nrows, ncols = table.get_shape()
    hr = None
    for i in range(min(nrows, 20)):
        if sum(1 for j in range(ncols)
               if table.get_cell(i, j).lower() == "материал") >= 2:
            hr = i
            break
    if hr is None:
        return []
    header = [table.get_cell(hr, j).lower() for j in range(ncols)]
    groups = [j for j in range(ncols)
              if header[j] in ("столбец 1", "наименование", "размер")
              and j + 1 < ncols and header[j + 1] == "материал"]
    if not groups:
        return []
    rows = []
    for i in range(hr + 1, nrows):
        for j in groups:
            name = table.get_cell(i, j)
            if not name or len(name) < 3:
                continue
            if not re.search(r"[A-Za-zА-Яа-я]", name) or not re.search(r"\d", name):
                continue
            low = name.lower()
            if any(w in low for w in ("итого", "всего", "цена", "наименование")):
                continue
            mat = table.get_cell(i, j + 1)
            th = None
            if j + 2 < ncols and header[j + 2] == "толщина":
                th = core.clean_num(table.get_cell(i, j + 2))
            stock = None
            if j + 3 < ncols and header[j + 3] in ("кол-во", "количество"):
                sv = core.clean_num(table.get_cell(i, j + 3))
                if sv is not None and sv >= 0:
                    stock = int(sv)
            nm = core.norm_text(name)
            rows.append(dict(
                SKU=f"PTFE-{re.sub(r'[^A-Za-z0-9]', '', nm).upper()}",
                Категория=core.CATEGORY_RENAMES.get(category, category),
                Тип_изделия="Шайба" if "шайба" in low else "Прокладка",
                Стандарт="", Маркировка_в_прайсе=name,
                **{"DN / Размер": "", "PN / Давление": ""},
                Материал=mat, **{"Толщина / Высота": th},
                Основные_размеры="", Вес_кг=None, Остаток=stock, Цена_без_НДС=None,
                Исполнение=None, Наполнитель=None,
                Источник=core.CUR["src"],
                Координата=f'{core.CUR["sheet"]}!{core.xl_cell(i, j)}'))
    return rows

_STOCK_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
# итерация 39: прокладки на листе «Кольца КГН» (граница слова/дефиса)
_KGN_GASKET_RE = re.compile(r"(?i)(?:^|[\s\-])(?:ПАГФ|ПУТГ)(?:$|[\s\-])")


def _kgn_stock(table, i, ncols):
    if ncols > 1:
        m = _STOCK_NUM_RE.search(table.get_cell(i, 1))
        if m:
            v = float(m.group(0).replace(",", "."))
            return int(v) if v >= 0 else None
    return None


def parse_kgn_rings(src) -> list:
    """Лист «Кольца КГН» (Price1): опорные/уплотнительные кольца и комплекты
    (ТМГ, КГФ, КГН и т.п.) — ассортимент без цен, по образцу parse_copper.
    Вторая колонка листа — остаток на складе (штуки, иногда с единицей
    измерения вроде «100шт») -> «Остаток»; Цена_без_НДС всегда None."""
    table = as_table(src)
    nrows, ncols = table.get_shape()
    rows = []
    seen = set()
    for i in range(nrows):
        name = table.get_cell(i, 0)
        if not name or len(str(name)) < 3:
            continue
        s = str(name).strip()
        if not re.search(r"\d", s) or not re.search(r"[A-Za-zА-Яа-я]", s):
            continue
        low = s.lower()
        if any(w in low for w in ("наименование", "итого", "ед.изм", "остаток")):
            continue
        key = core.norm_text(s)
        if key in seen:
            continue
        seen.add(key)
        if "комплект" in low or re.match(r"^к-\d", low):
            product_type = "Комплект"
        else:
            product_type = "Кольцо"
        # итерация 39: строки листа, являющиеся ПРОКЛАДКАМИ (ПАГФ/ПУТГ), а не
        # кольцами -> категория «Прокладки», материал «Графит»; поставщик
        # задаётся здесь (catalog.apply_supplier_overrides_v4 трогает только
        # «Кольца КГН»): ПУТГ — Vendor B, ПАГФ — Vendor C.
        if _KGN_GASKET_RE.search(s):
            rows.append(dict(
                SKU=f"GSK-{re.sub(r'[^A-Za-z0-9]', '', key)}",
                Категория="Прокладки", Тип_изделия="Прокладка",
                Стандарт="", Маркировка_в_прайсе=s,
                **{"DN / Размер": "", "PN / Давление": ""},
                Материал="Графит", **{"Толщина / Высота": None},
                Основные_размеры="", Вес_кг=None,
                Остаток=_kgn_stock(table, i, ncols), Цена_без_НДС=None,
                Поставщик="Vendor B" if "ПУТГ" in s.upper() else "Vendor C",
                Исполнение=None, Наполнитель=None,
                Источник=core.CUR["src"],
                Координата=f'{core.CUR["sheet"]}!{core.xl_cell(i, 0)}'))
            continue
        stock = None
        if ncols > 1:
            m = _STOCK_NUM_RE.search(table.get_cell(i, 1))
            if m:
                v = float(m.group(0).replace(",", "."))
                stock = int(v) if v >= 0 else None
        rows.append(dict(
            SKU=f"KGN-{re.sub(r'[^A-Za-z0-9]', '', key)}",
            Категория="Кольца КГН", Тип_изделия=product_type,
            Стандарт="", Маркировка_в_прайсе=s,
            **{"DN / Размер": "", "PN / Давление": ""},
            Материал="", **{"Толщина / Высота": None},
            Основные_размеры="", Вес_кг=None, Остаток=stock, Цена_без_НДС=None,
            Поставщик="поставщик не определён",
            Исполнение=None, Наполнитель=None,
            Источник=core.CUR["src"],
            Координата=f'{core.CUR["sheet"]}!{core.xl_cell(i, 0)}'))
    return rows


# ============================ VENDOR A / БРЕНД-ЭКВИВАЛЕНТЫ (итерация 31) ============================
# Не листы Excel — справочники src/data/*.json (core.load_vendor_a_prices/
# load_brand_equivalents). Возвращают СЫРЫЕ строки «Набивки» без сверки с уже
# собранным каталогом: какие бренды уже есть (цену не трогать) и какие новые
# знает только catalog.py (там уже собран df) — catalog.apply_vendor_a_prices/
# apply_brand_equivalents делают сверку и решают, добавлять строку или нет.

def parse_vendor_a_prices() -> list:
    """Таблица 1 (vendor_tables_2026-09.md) — прайс Vendor A/
    Vendor A, май 2025. МП 132 — пять строк по сечению (4 размера +
    «нестандартное сечение»): исходная строка 23 таблицы напечатана
    «НБСТАНД» в графе сечения — это опечатка от переноса печатной таблицы
    (значение графы «Сечение» вместо самого сечения), означает «НЕСТАНДАРТНОЕ
    СЕЧЕНИЕ», пятый вариант серии МП 132, НЕ отдельный бренд (уточнено
    пользователем после первой редакции, где НБСТАНД ошибочно завели как
    самостоятельную позицию)."""
    items = core.load_vendor_a_prices().get("items", [])
    supplier = core.load_vendor_a_prices().get(
        "supplier", "Vendor A")
    rows = []
    for it in items:
        brand, section, price, row_no = it["brand"], it["section"], it["price"], it["row"]
        if brand.strip() == "МП 132":
            mark = f"МП132 ({section})" if "нестандарт" in section.lower() \
                else f"МП132 {section}"
        else:
            mark = brand if brand.upper().startswith("НАБИВКА") else f"Набивка {brand}"
        rows.append(dict(
            SKU=f"PACKING-{core.norm_brand(mark)}",
            Категория="Набивки", Тип_изделия="Набивка", Стандарт="",
            Маркировка_в_прайсе=mark,
            **{"DN / Размер": "", "PN / Давление": ""},
            Материал="", **{"Толщина / Высота": None},
            Основные_размеры="", Вес_кг=None, Остаток=None,
            Цена_без_НДС=float(price),
            Поставщик=supplier,
            Исполнение=None, Наполнитель=None,
            Источник="VENDOR_A_MD",
            Координата=f"VENDOR_A_MD!строка {row_no}"))
    return rows


def parse_brand_equivalents() -> list:
    """Классы эквивалентности брендов (таблицы 2+3 markdown-таблиц поставщиков,
    core.load_brand_equivalents) как сырые строки «Набивки» — цена всегда
    NULL («цена по запросу»), поставщик по supplier_per_brand."""
    data = core.load_brand_equivalents()
    sup = data.get("supplier_per_brand", {})
    rows = []
    for cls in data.get("classes", []):
        cid = cls.get("class_id")
        for m in cls.get("members", []):
            brand, mark = m["brand"], m["mark"]
            rows.append(dict(
                SKU=f"PACKING-{core.norm_brand(mark)}",
                Категория="Набивки", Тип_изделия="Набивка", Стандарт="",
                Маркировка_в_прайсе=mark,
                **{"DN / Размер": "", "PN / Давление": ""},
                Материал="", **{"Толщина / Высота": None},
                Основные_размеры="", Вес_кг=None, Остаток=None,
                Цена_без_НДС=None,
                Поставщик=sup.get(brand, ""),
                Исполнение=None, Наполнитель=None,
                Источник="BRAND_EQUIV_MD",
                Координата=f"BRAND_EQUIV_MD!класс {cid}"))
    return rows