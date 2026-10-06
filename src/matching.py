# -*- coding: utf-8 -*-
"""src/matching.py — разбор заявок, точный подбор и подбор аналогов.

find_smart_analogs / find_cross_analogs / find_brand_analogs перенесены сюда
из gui.py в итерации 4 (заморозка matching.py снята).
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

import pandas as pd

import core

STEEL_RE = re.compile(r"(SS\s?304|SS\s?321|SS\s?316\s?L|SS\s?316\s?Ti|"
                      r"08Х18Н10Т|12Х18Н10Т|08Х13|09Г2С|08КП)", re.IGNORECASE)
QTY_RE = re.compile(r"(\d+)\s*(?:шт|pc|pcs)\b", re.IGNORECASE)

# ---- количество и единица (итерация 50, находка 2) ----
# «N шт/штук/кг/м/уп/компл», «x N» — не токен поиска, а количество: вырезаются
# из строки ДО любого маршрута (parse_customer_line, search_generic,
# search_with_criteria, nearest_variants, diagnose_search, audit_lines).
# Число обязано стоять отдельно: (?<![\w.,]) не даёт взять «6» из «6х6 шт»
# (старый QTY_RE так и делал: «МС101 6х6 шт» -> qty=6); «м» — только целым
# словом, «10мм»/«МПа» им не являются.
_QTY_UNIT = (r"(?:штук[аи]?|шт\.?|pcs|pc|кг|уп(?:ак(?:овк[аи])?)?\.?"
             r"|компл(?:ект(?:а|ов)?)?\.?|м)")
QTY_UNIT_RE = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*" + _QTY_UNIT + r"(?!\w)",
                         re.IGNORECASE)
_QTY_BARE_UNIT_RE = re.compile(
    r"(?<![\w.,])(?:штук[аи]?|шт\.?|pcs|кг|уп(?:ак(?:овк[аи])?)?\.?)(?!\w)", re.IGNORECASE)
_QTY_MULT_RE = re.compile(r"(?:^|\s)[xх×*]\s*(\d+)\s*$", re.IGNORECASE)
_PACKING_WORD_RE = re.compile(r"НАБИВК|САЛЬНИК")
_SECTION_RE = re.compile(r"\d\s*[XХ×]\s*\d", re.IGNORECASE)
_SECTION_MM_RE = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*мм(?!\w)", re.IGNORECASE)


def _qty_value(txt):
    v = float(str(txt).replace(",", "."))
    return int(v) if v.is_integer() else int(-(-v // 1))  # дробное -> вверх


def split_qty(line):
    """(строка без количества/единиц, qty или None). Последнее «N ед.» —
    количество; все такие выражения и голые «шт»/«кг»/«уп» вырезаются.
    «x N» в конце — количество, только если слева не голое число (иначе это
    сечение «6 х 6»)."""
    s = str(line)
    qty = None
    found = list(QTY_UNIT_RE.finditer(s))
    if found:
        qty = _qty_value(found[-1].group(1))
        s = QTY_UNIT_RE.sub(" ", s)
    s = _QTY_BARE_UNIT_RE.sub(" ", s)
    m = _QTY_MULT_RE.search(s)
    if m:
        prev = s[:m.start()].split()
        if not (prev and re.fullmatch(r"\d+(?:[.,]\d+)?", prev[-1])):
            if qty is None:
                qty = int(m.group(1))
            s = s[:m.start()]
    return re.sub(r"\s+", " ", s).strip(), qty


def normalize_request_line(line):
    """Единая нормализация строки заявки перед подбором (итерация 50):
    split_qty + сечение набивки «10мм» -> «10х10» (строка со словом
    «набивка»/«сальниковая» без явного сечения NxN — набивки квадратные;
    иначе parse_customer_line читал бы «10мм» как толщину и маршрут
    search_with_criteria отфильтровал бы набивки по «Толщина / Высота»,
    которой у них нет). Возвращает (текст, qty или None)."""
    s, qty = split_qty(line)
    if _PACKING_WORD_RE.search(s.upper()) and not _SECTION_RE.search(s):
        s = _SECTION_MM_RE.sub(lambda m: f"{m.group(1)}х{m.group(1)}", s, count=1)
    return s, qty
# необязательные числовые группы не должны захватывать целую часть дробного
# числа (запятая/точка) — иначе "...29-1,6-3,2" отдаёт dn=1 вместо dn=29
# (см. хотфикс 9.2: старые маркировки ОСТ "СНП В-3-29-1,6-3,2").
MARK_RE = re.compile(r"СНП[-\s]?([А-ЯA-Z])[-\s]?(\d+)"
                      r"(?:[-\s](\d+)(?![.,]\d))?(?:[-\s](\d+)(?![.,]\d))?")

# номер стандарта вида "26.260.454-99" (а также гибридные разделители
# "26.260-454-99"/"26.260.454.99" — итерация 16) — точка/дефис здесь
# разделители разделов номера, а не десятичный (десятичный разделитель
# размеров/давления в этом прайсе — запятая); токен целиком маскируется до
# разбора std/dn/pn/qty, чтобы суффикс "-99" не читался как количество,
# "26.260.454" — как размер, а "ОСТ26.260-454-99" (слитно, без пробела) не
# мешал детекции стандарта. (?<!\d) вместо ведущего \b — намеренно: между
# кириллической буквой ("Т" в "ОСТ") и цифрой нет word-границы (обе — \w в
# Unicode-режиме re), поэтому \b\d+ не матчился бы на слитном "ОСТ26...";
# детекция std от этого не страдает — она ищет "ОСТ" через `in` (подстрока),
# независимо от того, что происходит дальше в строке.
# ПЕРВЫЙ разделитель — ОБЯЗАТЕЛЬНО точка (дальше — точка ИЛИ дефис): иначе
# обычные дефисные маркировки СНП вида "2-3-250-16" (только дефисы, без
# точек вовсе) тоже попали бы под маску — это была бы регрессия почти всех
# СНП-заявок, а не укрепление маски стандарта.
STD_NUM_RE = re.compile(r"(?<!\d)\d+\.\d+(?:[.\-]\d+)+\b")

# голые коды стали без "SS"-префикса (итерация 9) — только эти 4, поиск
# словными границами, чтобы не задеть "DN321"/"PN321" (там нет границы
# перед цифрой) и не подхватить произвольные числа.
BARE_STEEL_RE = re.compile(r"\b(304|321|316L|316Ti)\b", re.IGNORECASE)
_BARE_STEEL_CANON = {"304": "304", "321": "321", "316L": "316L", "316TI": "316Ti"}

# дюймовые метки ASME (2", 2 1/2", 2″, 2 in) -> DN через обратное core.DN_TO_INCH
INCH_RE = re.compile(r'(\d+(?:\s+\d+/\d+)?|\d+/\d+)\s*(?:"|″|IN\b)')

# плейсхолдеры старых маркировок вида "...-(Ру)-(h)" (место для давления/
# высоты, не значение) — итерация 12, R.реальный-промах: маскируются вместе
# со STEEL_RE/QTY_RE/STD_NUM_RE перед fallback-разбором pn из хвоста.
_PN_PLACEHOLDER_RE = re.compile(r"\((?:РУ|RU|H|R)\)", re.IGNORECASE)

# итерация 38: написания плейсхолдера давления (Ру)/(Py)/Py/Ру (кириллица/
# латиница, регистр любой, скобки опциональны) эквивалентны — канон каталога
# "(Ру)". Граница (?<!\w)/(?!\w): «Ру16» (значение давления) и слова с «ру»
# внутри не задеваются; (h) и прочие токены не трогаются. Хранимые маркировки
# не меняются — только разбор заявки и ключ сравнения.
_PN_PH_ANY_RE = re.compile(r"(?<!\w)\(?(?:РУ|PY)\)?(?!\w)", re.IGNORECASE)
PN_PLACEHOLDER_CANON = "(Ру)"

# итерация 22 (реальный промах ANSI/ASME-заявки): «КЛАСС ДАВЛЕНИЯ 300-600»
# (single или диапазон, как в самом прайсе — лист «ASME B 16.20» хранит и
# одиночные классы, и текстовые диапазоны «(300....600)» для тех же ячеек).
# Числа ограничены известными классами (core.ANSI_CLASS_TO_KGS), а не «любые
# цифры» — иначе «КЛАСС ДАВЛЕНИЯ 900 - 2 шт.» без размеров между классом и
# количеством жадно читало бы «- 2» как верхнюю границу диапазона класса.
_CLASS_ALT = "|".join(str(c) for c in sorted(core.ANSI_CLASS_TO_KGS, reverse=True))
_CLASS_RE = re.compile(rf"КЛАСС\s+ДАВЛЕНИЯ\s+({_CLASS_ALT})(?:\s*[-–]\s*({_CLASS_ALT}))?",
                       re.IGNORECASE)
# размеры D2/D3/D4 заявки клиента вида "D2-101,6(ММ)" — запятая (десятичный
# разделитель размеров в прайсе), "(ММ)" опционален. Латинская D — сознательно
# отдельно от кириллической «Д» (буква типа изделия), совпадений не бывает.
DIMS_REQ_RE = re.compile(r"\bD([234])[-\s]*(\d+(?:[.,]\d+)?)\s*(?:\(ММ\)|MM)?", re.IGNORECASE)
# стык исполнений СНП, клиентская нотация (итерация 26, SELECTION_SPEC,
# раздел «Стыки исполнений»): буквенный токен ПОСЛЕ буквы типа и ПЕРЕД
# числами DN/PN — «В-В»/«B-B» (кириллица и латиница), «E-F», «C-D».
# ЛИТЕРАЛЬНАЯ альтернация (а не общий класс букв [A-Za-zА-Яа-я]) — специально
# НЕ матчит произвольные пары букв вроде «Е-Г» в репро-строке
# «СНП-А-Е-Г-15-40»: это сохраняет прежнее (правильное) поведение
# «не распознано» для строк, где буквенная пара не входит ни в один
# известный стык, вместо того чтобы регэксп проглотил её как валидный токен
# и затем полагался на код после матча отбраковывать бессмысленную пару.
JOINT_MARK_RE = re.compile(
    r"СНП[-\s]?([А-ЯA-Z])[-\s]"
    r"(В[-\s]В|B[-\s]B|E[-\s]F|C[-\s]D)[-\s]"
    r"(\d+)(?![.,]\d)(?:[-\s](\d+)(?![.,]\d))?"
)
_JOINT_SEP_RE = re.compile(r"[-\s]+")
_JOINT_RAW_CODES = {"ВВ": "BB", "BB": "BB", "EF": "EF", "CD": "CD"}


def _norm_joint(raw):
    """Буквенный токен JOINT_MARK_RE -> core.JOINT_EXEC_PAIRS/JOINT_TYPE_FAMILY
    код ("BB"/"EF"/"CD"), без учёта разделителя/кириллицы-латиницы."""
    return _JOINT_RAW_CODES.get(_JOINT_SEP_RE.sub("", str(raw).upper()))


def _joint_label(code):
    return core.JOINT_LABELS.get(code, code)


# тип лица фланца ASME (те же токены, что parsers.py::_ASME_FLANGE, но
# отдельная копия — parsers.py не экспортирует regex, читать/менять его в
# этой итерации нельзя) — нужен только для восстановления префикса
# маркировки под сравнение (resolve_customer), к разбору std/dn отношения
# не имеет.
_ASME_FLANGE_RE = re.compile(r"\b(RF|FF|RJ|RTJ|STG|LTG|LMF|SMF|TG|MF|GF)\b")


def _inch_to_float(s):
    total, got = 0.0, False
    for part in str(s).strip().split():
        if "/" in part:
            num, _, den = part.partition("/")
            try:
                total += float(num) / float(den)
                got = True
            except (ValueError, ZeroDivisionError):
                return None
        else:
            try:
                total += float(part)
                got = True
            except ValueError:
                return None
    return total if got else None


def _build_inch_to_dn():
    out = {}
    for dn, label in core.DN_TO_INCH.items():
        v = _inch_to_float(str(label).replace('"', "").strip())
        if v is not None:
            out[round(v, 4)] = dn
    return out


_INCH_TO_DN = _build_inch_to_dn()


def _num(s):
    m = re.search(r"\d+(?:[.,]\d+)?", str(s))
    return float(m.group(0).replace(",", ".")) if m else None


# ============================ DN ============================

def enrich_dn(df):
    """Колонка DN_num: ГОСТ/ОСТ — целое из 'DN / Размер'; ASME — обратное DN_TO_INCH."""
    if df is None or not len(df) or "DN / Размер" not in df.columns:
        return df
    inv = {str(v).strip(): k for k, v in core.DN_TO_INCH.items()}
    df = df.copy()
    dn = []
    for s, std in zip(df["DN / Размер"], df["Стандарт"]):
        s = "" if s is None or pd.isna(s) else str(s)
        std = "" if std is None or pd.isna(std) else str(std)
        if std == "ASME":
            dn.append(inv.get(s.strip()))
        else:
            m = re.search(r"\d+", s)
            dn.append(int(m.group(0)) if m else None)
    df["DN_num"] = dn
    return df


# ============================ ДАВЛЕНИЕ ============================

_MPA_TEXT_CACHE: dict = {}


def row_pressure_kgs(row):
    """(lo, hi, rated) — диапазон давления строки в кгс/см² (итерация 50,
    находка 1). Сначала числовые «Давление_кгс_мин/макс» (СНП ГОСТ/ОСТ).
    У категорий core.PRESSURE_TEXT_MPA_CATEGORIES (кольца овальные/
    восьмиугольные) эти колонки пусты по всему каталогу, давление только
    текстом «PN / Давление» в МПа («6.3-10», «16») — читается здесь тем же
    core.pressure_range (×10), что заполняет СНП при сборке; каталог не
    меняется. rated=True — давление для строки ОБЯЗАТЕЛЬНО: без него строка
    при заданном PN фильтр не проходит. Итерация 50b: только кольца ГОСТ/ОСТ;
    кольца ASME (R-номер, давления нет по определению) идут маршрутом
    search_rings и давлением не фильтруются. У
    прочих строк без диапазона (СНП ASME — класс, набивки и т.п.) rated=False,
    прежнее поведение «проходит всегда»."""
    lo, hi = row.get("Давление_кгс_мин"), row.get("Давление_кгс_макс")
    lo = _num(lo) if lo is not None and str(lo) not in ("", "nan") else None
    hi = _num(hi) if hi is not None and str(hi) not in ("", "nan") else None
    if lo is not None or hi is not None:
        return lo, hi, True
    if (str(row.get("Категория") or "") in core.PRESSURE_TEXT_MPA_CATEGORIES
            and str(row.get("Стандарт") or "") != "ASME"):
        txt = row.get("PN / Давление")
        txt = "" if txt is None or pd.isna(txt) else str(txt).strip()
        if txt not in _MPA_TEXT_CACHE:
            _MPA_TEXT_CACHE[txt] = core.pressure_range(txt)[2:] if txt else (None, None)
        klo, khi = _MPA_TEXT_CACHE[txt]
        return klo, khi, True
    return None, None, False


def pressure_hit(row, pn_kgs):
    """Запросное давление (кгс/см2) попадает в диапазон строки.
    Строки без диапазона: СНП ASME и категории без давления — проходят
    всегда; кольца без распознаваемого давления — НЕ проходят (итерация 50)."""
    if pn_kgs is None:
        return True
    lo, hi, rated = row_pressure_kgs(row)
    if lo is None and hi is None:
        return not rated
    lo = lo if lo is not None else hi
    hi = hi if hi is not None else lo
    return lo - 1e-9 <= float(pn_kgs) <= hi + 1e-9


def pressure_mask(df, pn_kgs):
    # dtype=bool ЯВНО: на уже пустом (0 строк) df список в pd.Series([]) без
    # dtype получает "object", и индексация df[mask] с object-маской вместо
    # bool отдаёт DataFrame с 0 КОЛОНКАМИ (не только 0 строк) — тот самый
    # KeyError('Категория') ниже по цепочке (хотфикс 9.2, root-cause).
    if pn_kgs is None:
        return pd.Series(True, index=df.index, dtype=bool)
    return pd.Series([pressure_hit(r, pn_kgs) for _, r in df.iterrows()],
                     index=df.index, dtype=bool)


def pressure_range_hit(row, pn_lo, pn_hi):
    """Диапазон запроса [pn_lo, pn_hi] (кгс) пересекается с диапазоном строки.
    Строки без диапазона — как в pressure_hit (row_pressure_kgs, итерация 50)."""
    lo, hi, rated = row_pressure_kgs(row)
    if lo is None and hi is None:
        return not rated
    lo = lo if lo is not None else hi
    hi = hi if hi is not None else lo
    return lo <= float(pn_hi) + 1e-9 and float(pn_lo) <= hi + 1e-9


def pressure_range_mask(df, pn_lo, pn_hi):
    return pd.Series([pressure_range_hit(r, pn_lo, pn_hi) for _, r in df.iterrows()],
                     index=df.index, dtype=bool)


def _dims_hit(dims_txt, dims_req) -> bool:
    d = core.norm_dims(dims_txt)
    for k, v in dims_req.items():
        if k not in d or abs(float(d[k]) - float(v)) > core.CROSS_TOL:
            return False
    return True


def dims_mask(df, dims_req):
    """Фильтр по размерам D2/D3/D4 заявки ANSI (итерация 22): совпадение
    core.norm_dims(Основные_размеры) по ВСЕМ ключам dims_req в допуске
    core.CROSS_TOL. Список по df.iterrows() вместо .map() на str-колонке —
    тот же паттерн, что pressure_mask/pressure_range_mask (pandas 3.0,
    CLAUDE.md: .map() на строковых колонках запрещён)."""
    return pd.Series([_dims_hit(v, dims_req) for v in df["Основные_размеры"]],
                     index=df.index, dtype=bool)


# ============================ РАЗБОР ЗАЯВКИ ============================

def has_criteria(cust) -> bool:
    """True, если в заявке есть хоть один признак для resolve_customer —
    предохранитель маршрутизации (хотфикс 9.2): без этого пустой cust молча
    выбрал бы «весь каталог» (ни один if в resolve_customer не сработал бы)."""
    return bool(cust.get("mark") or cust.get("type") or cust.get("steel")
                or cust.get("dn") is not None or cust.get("pn") is not None
                or cust.get("pn_lo") is not None or cust.get("thickness") is not None)


def has_dn_pn_steel_thickness(cust) -> bool:
    """True, если распознан хоть один из dn/pn/pn_lo/steel/thickness —
    условие маршрутизации на search_with_criteria (итерация 18): строка без
    маркировки/типа/«СНП» (routed_resolve уже False), но со структурными
    критериями, которых обычный search_generic не видит (его hay —
    маркировка+SKU+материал+категория, без давления/DN как чисел) — топ-
    причина промахов аудита боевого журнала (итерация 17)."""
    return bool(cust.get("dn") is not None or cust.get("pn") is not None
                or cust.get("pn_lo") is not None or cust.get("steel")
                or cust.get("thickness") is not None)


def request_route(cust) -> str:
    """Маршрут строки заявки (та же развилка, что gui.OrderTab.on_resolve/
    entry.cmd_search): "resolve" — маркировка/тип СНП с признаками,
    "criteria" — search_with_criteria, "generic" — search_generic. Итерация
    50: нужен КП (kp_export.kp_name — маркировка клиента только у "resolve")."""
    raw = str(cust.get("raw") or "")
    routed = bool(cust.get("mark") or cust.get("type") or "СНП" in raw.upper())
    if routed and has_criteria(cust):
        return "resolve"
    if not routed and has_dn_pn_steel_thickness(cust):
        return "criteria"
    return "generic"


def parse_customer_line(line):
    s = str(line).strip()
    cust = {"raw": s, "mark": None, "type": None, "std": None, "dn": None,
            "pn": None, "pn_lo": None, "pn_hi": None, "thickness": None,
            "steel": None, "qty": 1, "class_lo": None, "class_hi": None,
            "dims_req": {}, "joint": None}
    # итерация 50: количество/единицы («2 шт», «3 кг», «x 4») вырезаются до
    # разбора — единая точка normalize_request_line для всех маршрутов.
    s_clean, qty_explicit = normalize_request_line(s)
    if qty_explicit is not None:
        cust["qty"] = qty_explicit
    # номер стандарта ("ОСТ 26.260.454-99") маскируем до разбора чисел —
    # иначе его "-99" читается как кол-во, а "26.260.454" — как размер.
    work = _PN_PH_ANY_RE.sub(PN_PLACEHOLDER_CANON, STD_NUM_RE.sub(" ", s_clean))
    up = work.upper()
    # ANSI — обиходное имя того же стандарта, что каталог хранит как "ASME"
    # (итерация 22); служебные токены фланца/лица (RF/FF/…) здесь ни на что
    # не влияют — детект std ищет только "ГОСТ"/"ОСТ"/"ANSI"/"ASME" подстроками.
    for std, canon in (("ГОСТ", "ГОСТ"), ("ОСТ", "ОСТ"), ("ANSI", "ASME"), ("ASME", "ASME")):
        if std in up:
            cust["std"] = canon
            break
    m = STEEL_RE.search(work)
    if m:
        cust["steel"] = core.mat_norm(m.group(1))
    if cust["steel"] is None:
        # голый код без "SS" (итерация 9): "...321" -> SS321 и т.п.
        m = BARE_STEEL_RE.search(work)
        if m:
            canon = _BARE_STEEL_CANON.get(m.group(1).upper())
            if canon:
                cust["steel"] = core.mat_norm(canon)
    m = re.search(r"толщ[.:]?\s*([\d.,]+)|[-\s]([\d.,]+)\s*мм", work, re.IGNORECASE)
    if m:
        cust["thickness"] = _num(m.group(1) or m.group(2))
    # DN N / Ду N / DU N — явный номер, ВСЕГДА в приоритете (даже над
    # дюймовым пересчётом ниже и над числом, которое позже возьмёт MARK_RE
    # из хвоста маркировки) — explicit_dn помечает это для финального
    # дюймового шага (итерация 22).
    explicit_dn = None
    m = re.search(r"\bDN\s*(\d+)", up) or re.search(r"\b(?:ДУ|DU)\s*(\d+)", up)
    if m:
        explicit_dn = int(m.group(1))
        cust["dn"] = explicit_dn
    # давление: число + окно единиц. МПа/MPa -> х10 (кгс); кгс/kgf/без единицы -> как есть
    m = re.search(r"\bPN\s*([\d.,]+)\s*(МПА|MPA|КГС|KGF)?\b", up)
    if m:
        val = _num(m.group(1))
        if val is not None:
            if m.group(2) in ("МПА", "MPA"):
                val *= 10.0
            cust["pn"] = val
    # стык исполнений (итерация 26): «SNP-<тип>-<стык>-<DN>[-<PN>]» — при
    # успехе даёт type/dn/pn/joint НАПРЯМУЮ и НЕ проходит через MARK_RE (та
    # же строка не матчит MARK_RE вовсе — после буквы типа там буквы стыка,
    # не цифры). mark сознательно остаётся None: каталожная маркировка этих
    # строк использует ЧИСЛОВОЕ исполнение ("СНП-Г-1-1-10-(Ру)"), а не
    # буквенный стык клиента, префиксное сравнение с ней всё равно ничего не
    # найдёт — подбор целиком идёт через type+joint+dn+pn (matching.resolve_
    # customer), как у ANSI-заявок с dims_req (dn/pn всё ещё уступают явному
    # DN N/PN N выше по приоритету, см. explicit_dn/cust["pn"] guard).
    jm = JOINT_MARK_RE.search(up)
    if jm:
        cust["type"] = jm.group(1)
        cust["joint"] = _norm_joint(jm.group(2))
        if explicit_dn is None:
            cust["dn"] = int(jm.group(3))
        if jm.group(4) and cust["pn"] is None:
            cust["pn"] = float(jm.group(4))
        m = None
    else:
        m = MARK_RE.search(up)
    if m:
        cust["type"] = m.group(1)
        cust["mark"] = m.group(0).replace(" ", "")
        nums = [int(x) for x in m.groups()[1:] if x]
        if nums:
            cust["dn"] = nums[-1]
        tail = up[m.end():]
        # диапазон давления сразу после маркировки: "lo[-–]hi" (числа с
        # запятой) — старые маркировки ОСТ вида "...-29-1,6-3,2 ОСТ" хранят
        # давление в МПа диапазоном, а не одним числом в кгс.
        range_m = re.match(r"\s*[-–]?\s*(\d+(?:[.,]\d+)?)\s*[-–]\s*"
                           r"(\d+(?:[.,]\d+)?)\s*(МПА|MPA|КГС|KGF)?", tail)
        if range_m:
            lo, hi = _num(range_m.group(1)), _num(range_m.group(2))
            unit = range_m.group(3)
            if unit in ("МПА", "MPA"):
                factor = 10.0
            elif unit in ("КГС", "KGF"):
                factor = 1.0
            elif cust["std"] == "ОСТ":
                factor = 10.0          # умолчание по стандарту: ОСТ хранит PN в МПа
            else:
                factor = 1.0           # ГОСТ и прочие умолчания — кгс (сохранение семантики)
            if lo is not None and hi is not None:
                cust["pn_lo"] = min(lo, hi) * factor
                cust["pn_hi"] = max(lo, hi) * factor
        elif cust["pn"] is None:
            # fallback-давление (итерация 12): голый номер сразу после хвоста
            # маркировки — но только когда это ДЕЙСТВИТЕЛЬНО число PN, а не
            # цифры кода стали/кол-ва/плейсхолдера, случайно оказавшиеся
            # первыми в tail. Был баг: "...(SS304) 5 шт" -> pn=304 (число из
            # "SS304"), потому что _num(tail) искал первую цифру ГДЕ УГОДНО.
            # Маскируем STEEL_RE/QTY_RE/STD_NUM_RE/плейсхолдеры "(РУ)"/"(H)"/
            # "(RU)"/"(R)" и требуем, чтобы число стояло СРАЗУ после хвоста
            # (re.match, не re.search) — иначе pn остаётся не распознан.
            tail_masked = STEEL_RE.sub(" ", tail)
            tail_masked = QTY_RE.sub(" ", tail_masked)
            tail_masked = STD_NUM_RE.sub(" ", tail_masked)
            tail_masked = _PN_PLACEHOLDER_RE.sub(" ", tail_masked)
            pn_m = re.match(r"[\s\-–,()]*(\d+(?:[.,]\d+)?)", tail_masked)
            if pn_m:
                cust["pn"] = _num(pn_m.group(1))
    # «КЛАСС ДАВЛЕНИЯ X[-Y]» (итерация 22, ANSI/ASME) — числа классов
    # 150/300/400/600/900/1500/2500, как на листе «ASME B 16.20».
    cm = _CLASS_RE.search(up)
    if cm:
        lo = int(cm.group(1))
        hi = int(cm.group(2)) if cm.group(2) else lo
        cust["class_lo"], cust["class_hi"] = min(lo, hi), max(lo, hi)
    # D2-101,6(ММ), D3-…, D4-… (итерация 22, ANSI) — запятая -> точка через _num.
    dims_req = {}
    for dm in DIMS_REQ_RE.finditer(up):
        val = _num(dm.group(2))
        if val is not None:
            dims_req[f"d{dm.group(1)}"] = val
    cust["dims_req"] = dims_req
    # дюймовый DN (итерация 22): выполняется ПОСЛЕДНИМ и ПЕРЕБИВАЕТ dn,
    # который MARK_RE выше мог успеть выставить по номеру исполнения из
    # усечённой маркировки (реальный промах: "СНП-Г-3-ANSI-RF-3"..." —
    # MARK_RE матчит только "СНП-Г-3", "3" здесь номер исполнения, а не DN;
    # дюйм 3" стоит дальше в строке, после нераспознанного "-ANSI-").
    # Явный DN/ДУ (explicit_dn) в приоритете — дюйм его не трогает.
    if explicit_dn is None and cust["std"] in (None, "ASME"):
        im = INCH_RE.search(up)
        if im:
            v = _inch_to_float(im.group(1))
            dn_val = _INCH_TO_DN.get(round(v, 4)) if v is not None else None
            if dn_val is not None:
                cust["dn"] = dn_val
                # префикс маркировки для сравнения (resolve_customer) должен
                # соответствовать формату каталога "СНП <тип> <лицо> N""
                # (разведка: лист ASME B 16.20 / Маркировка_в_прайсе), а не
                # усечённому "СНП-Г-3" — каталог никогда не пишет номер
                # исполнения перед лицом фланца у ASME-позиций.
                if cust["std"] == "ASME" and cust["mark"] and cust["type"]:
                    fm = _ASME_FLANGE_RE.search(up)
                    face = fm.group(1) if fm else ""
                    inch_label = im.group(1).strip() + '"'
                    cust["mark"] = (f"СНП-{cust['type']}-{face}-{inch_label}" if face
                                    else f"СНП-{cust['type']}-{inch_label}")
    if qty_explicit is None and (cust["dn"] is not None or cust["pn"] is not None):
        # R7 (итерация 12): хвостовое число — количество, только если оно
        # отделено пробелом от остального текста («...16» != qty=16 из
        # маркировки; «...16 2» -> qty=2 отдельным числом).
        m = re.search(r"(?<=\s)(\d+)\s*$", work)
        if m:
            cust["qty"] = int(m.group(1))
    return cust


# ============================ ТОЧНЫЙ ПОДБОР ============================

# Компаратор маркировок (итерация 16, реальный промах менеджера): заявка и
# прайс пишут одну и ту же маркировку то через дефис («СНП-Д-3-28»), то через
# пробел («СНП Д-3-28») — сравнение только по пробелам ложно обнуляло подбор
# («фильтр «маркировка» обнулил подбор» при реально существующей позиции).
# UPPER + схлопнуть пробелы/дефисы делает сравнение нечувствительным к
# разделителю. Ложные префиксные совпадения («СНП-Д-3-28» зацепит «…3-280…»)
# защищает НЕ эта нормализация, а последующий фильтр DN_num == cust["dn"] —
# сравнение маркировки здесь ОСОЗНАННО широкое (только префикс, без учёта
# разделителей), см. SELECTION_SPEC §2 (A7).
_MARK_CMP_STRIP_RE = re.compile(r"[\s\-]+")


def _norm_mark_cmp(s) -> str:
    """Скаляр (cust["mark"]): UPPER + без пробелов/дефисов."""
    return _MARK_CMP_STRIP_RE.sub("", _PN_PH_ANY_RE.sub("(РУ)", str(s).upper()))


def _norm_mark_series(s: pd.Series) -> pd.Series:
    """Векторный эквивалент _norm_mark_cmp для колонки каталога — тот же
    паттерн, без .map()/.apply() на str-колонке (см. CLAUDE.md, pandas 3.0:
    .map() на dtype "str" тихо возвращает None->NaN обратно)."""
    return (s.fillna("").astype(str).str.upper()
             .str.replace(_PN_PH_ANY_RE.pattern, "(РУ)", regex=True)
             .str.replace(_MARK_CMP_STRIP_RE.pattern, "", regex=True))


def resolve_customer(df, cust):
    # fillna("") перед astype(str): под pandas 3.0 сравнение/startswith на
    # NA даёт NA, а не False, и булева маска с NA валит индексацию
    # (res[mask]) исключением — тот же паттерн, что в search_generic/db.py.
    res = df
    if cust.get("mark"):
        norm = _norm_mark_series(res["Маркировка_в_прайсе"])
        res = res[norm.str.startswith(_norm_mark_cmp(cust["mark"]))]
        # fullmetal-группа (итерация 17, скрытие ОТМЕНЕНО итерацией 27): у
        # ГОСТ Г/Д второй, более дорогой вариант той же позиции —
        # "(ограничитель как навитая часть)" (core.GROUP_FULLMETAL) —
        # раньше скрывался при подборе по умолчанию как «дубль»; разведка
        # итерации 27 (лист "СНП-ГОСТ-Г"/"...-Д", колонки H-K «с
        # ограничителем из углеродки» и L-O «с ограничителем как навитая
        # часть») показала, что это ДВА РАЗНЫХ ПРОДУКТА с разными ценами, а
        # не дубль одной позиции — скрытие теряло легитимные варианты
        # подбора. По умолчанию показываются ОБЕ группы; различимость —
        # колонка «Ограничитель» (см. restrictor_kind) в GUI. Явное «как
        # навитая» в тексте заявки (cust["raw"]) — прежнее, НАМЕРЕННОЕ
        # требование клиента: сужает подбор ТОЛЬКО до fullmetal-строк, если
        # такие в выборке вообще есть.
        if len(res):
            wants_fullmetal = "как навитая" in str(cust.get("raw", "")).lower()
            if wants_fullmetal:
                is_fullmetal = (res["Маркировка_в_прайсе"].fillna("").astype(str)
                                .str.contains("как навитая", na=False))
                if is_fullmetal.any():
                    res = res[is_fullmetal]
    elif cust.get("type") and len(res):
        # итерация 26: joint-based строки (JOINT_MARK_RE) задают type БЕЗ
        # mark (каталожная маркировка этих строк числовая — "СНП-Г-1-1-10-
        # (Ру)", а не буквенный стык клиента — префиксное сравнение с ней
        # ничего не найдёт, см. комментарий у JOINT_MARK_RE) — симметричная
        # ветка той же fullmetal-развилки (A8), т.к. mark-ветка выше её не
        # покрывает. Итерация 27: то же правило — по умолчанию обе группы,
        # явный маркер «как навитая» в заявке сужает до fullmetal.
        wants_fullmetal = "как навитая" in str(cust.get("raw", "")).lower()
        if wants_fullmetal:
            is_fullmetal = (res["Маркировка_в_прайсе"].fillna("").astype(str)
                            .str.contains("как навитая", na=False))
            if is_fullmetal.any():
                res = res[is_fullmetal]
    if cust.get("std"):
        res = res[res["Стандарт"].fillna("").astype(str) == cust["std"]]
    if cust.get("type"):
        res = res[res["Тип_изделия"].fillna("").astype(str).str.upper() == cust["type"].upper()]
    if cust.get("joint") and "Исполнение" in res.columns:
        # итерация 26: стык — фильтр по паре исполнения маркировки
        # (core.JOINT_EXEC_PAIRS), применяется ТОЛЬКО когда cust не задал
        # явные числовые исполнения отдельно (эта ветка разбора — JOINT_
        # MARK_RE — как раз не пересекается со старым числовым MARK_RE,
        # см. parse_customer_line). У ОСТ/ASME колонка «Исполнение» не
        # заполнена (core.EXEC_TO_JOINT/JOINT_EXEC_PAIRS — разведка core.py)
        # — joint-фильтр естественно даёт 0 для этих строк, если стандарт не
        # сужен до ГОСТ раньше по цепочке.
        pair_strs = {f"{a}-{b}" for a, b in core.JOINT_EXEC_PAIRS.get(cust["joint"], set())}
        res = res[res["Исполнение"].fillna("").astype(str).isin(pair_strs)]
    dims_req = cust.get("dims_req") or {}
    if len(dims_req) >= 2:
        # итерация 22 (ANSI/ASME): размеры точнее номера DN — у одного
        # дюймового DN уживаются разные исполнения (STG/LTG и т.п.) с
        # разными D2/D3/D4; фильтр по dn здесь НЕ применяется вовсе.
        res = res[dims_mask(res, dims_req)]
    elif cust.get("dn") is not None and "DN_num" in res.columns:
        res = res[pd.to_numeric(res["DN_num"], errors="coerce") == cust["dn"]]
    if cust.get("pn_lo") is not None and cust.get("pn_hi") is not None:
        res = res[pressure_range_mask(res, cust["pn_lo"], cust["pn_hi"])]
    elif cust.get("pn") is not None:
        res = res[pressure_mask(res, cust["pn"])]
    elif cust.get("class_lo") is not None:
        # итерация 22: класс давления ANSI -> кгс-эквивалент по факту
        # кодирования каталога (core.ANSI_CLASS_TO_KGS, см. комментарий
        # константы) — переиспользует pressure_range_mask, включая случай
        # одиночного класса (lo==hi) и NaN-давление (строки без диапазона
        # проходят всегда, как у прочих ASME-позиций).
        kgs_lo = core.ANSI_CLASS_TO_KGS.get(cust["class_lo"])
        kgs_hi = core.ANSI_CLASS_TO_KGS.get(cust.get("class_hi") or cust["class_lo"])
        if kgs_lo is not None and kgs_hi is not None:
            res = res[pressure_range_mask(res, kgs_lo, kgs_hi)]
    if cust.get("thickness") is not None:
        th = pd.to_numeric(res["Толщина / Высота"], errors="coerce")
        res = res[(th - cust["thickness"]).abs() < 1e-9]
    if cust.get("steel"):
        res = res[res["Материал"].fillna("").astype(str).map(core.mat_norm)
                  == core.mat_norm(cust["steel"])]
    return res


def sort_for_manager(df: pd.DataFrame) -> pd.DataFrame:
    """Стабильная сортировка точной таблицы для менеджера (итерация 10):
    (Остаток > 0) desc -> (Поставщик содержит core.SUPPLIER_OWN_MARK) desc ->
    Цена_без_НДС asc (NaN — в конец группы). Состав/значения строк не меняет,
    только порядок; фильтры resolve_customer не затрагивает.
    Итерация 31: строки с NULL-ценой (бренд-эквиваленты «цена по запросу»,
    core.load_brand_equivalents) уже безопасны без правок — na_position=
    "last" на колонке _price уводит их в конец своей группы наличия/tier,
    так же как раньше уводил обычный отсутствующий Цена_без_НДС; tier
    «own production» (SUPPLIER_OWN_MARK) с этой же итерации фактически
    всегда пуст — «OWN (собств. производство)» больше нигде не проставляется
    (см. core.SUPPLIER_OWN_MARK), сортировка молча сводится к наличие->цена."""
    if df is None or not len(df):
        return df
    stock = pd.to_numeric(df.get("Остаток"), errors="coerce")
    own = (df.get("Поставщик", pd.Series("", index=df.index)).fillna("")
           .astype(str).str.contains(re.escape(core.SUPPLIER_OWN_MARK), regex=True))
    price = pd.to_numeric(df.get("Цена_без_НДС"), errors="coerce")
    tmp = df.assign(_no_stock=~(stock > 0).fillna(False),
                    _not_own=~own,
                    _price=price)
    tmp = tmp.sort_values(["_no_stock", "_not_own", "_price"],
                          kind="mergesort", na_position="last")
    return tmp.drop(columns=["_no_stock", "_not_own", "_price"])


def group_by_line(items):
    """Группирует плоский список (line_no, line, row) по (line_no, line) —
    порядок групп = порядок первых вхождений (совпадает с порядком ввода
    заявки, если items формируются построчным проходом). Ключ — ПАРА
    (line_no, line), а не текст маркировки: две строки заявки с одинаковым
    текстом, но разным номером (например, один и тот же артикул на разное
    давление), дают ДВЕ разные группы, не сливаются в одну.

    Единственный источник группировки для дерева точных позиций в GUI
    (итерация 13) — OrderTab использует только эту функцию, своей
    группировки не делает."""
    order = []
    groups = {}
    for line_no, line, row in items:
        key = (line_no, line)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    return [{"line_no": ln, "line": line, "rows": groups[(ln, line)]} for ln, line in order]


def find_snp(df, cust):
    res = resolve_customer(df, cust)
    if cust.get("mark") or cust.get("type"):
        # guard: вырожденный df без колонки "Категория" (защита от падений,
        # хотфикс 9.2) — считаем результат пустым, а не роняем KeyError.
        if len(res) and "Категория" in res.columns:
            res = res[res["Категория"].fillna("").astype(str) == "СНП"]
        elif len(res):
            res = res.iloc[0:0]
    return res


# ============================ ДИАГНОСТИКА ПРОМАХА ============================
# Шаги зеркалят resolve_customer один в один (тот же порядок, те же условия
# применения и та же fillna("")-защита) — отдельные функции, чтобы ничего в
# resolve_customer не трогать и не рисковать регрессией точного подбора.

_MISS_STEPS = [("mark", "маркировка"), ("std", "стандарт"), ("type", "тип"),
               ("joint", "стык исполнения"),
               ("dims", "размеры"), ("dn", "DN"), ("pn", "давление"),
               ("class", "класс давления"), ("thickness", "толщина"),
               ("steel", "сталь")]


def _miss_step_applies(cust, key):
    if key in ("mark", "std", "type", "steel", "joint"):
        return bool(cust.get(key))
    if key == "dims":
        return len(cust.get("dims_req") or {}) >= 2
    if key == "dn":
        # итерация 22: размеры точнее DN в ANSI — если применялся шаг
        # "dims", шаг "DN" пропускается (симметрично resolve_customer).
        return cust.get("dn") is not None and len(cust.get("dims_req") or {}) < 2
    if key == "pn":
        return cust.get("pn") is not None or (
            cust.get("pn_lo") is not None and cust.get("pn_hi") is not None)
    if key == "class":
        # класс — фолбэк ТОЛЬКО когда pn не задан (симметрично elif-цепочке
        # resolve_customer: pn_lo/hi -> pn -> class_lo).
        return (cust.get("class_lo") is not None and cust.get("pn") is None
                and not (cust.get("pn_lo") is not None and cust.get("pn_hi") is not None))
    return cust.get(key) is not None


def _miss_apply_step(res, cust, key):
    if key == "mark":
        norm = _norm_mark_series(res["Маркировка_в_прайсе"])
        return res[norm.str.startswith(_norm_mark_cmp(cust["mark"]))]
    if key == "std":
        return res[res["Стандарт"].fillna("").astype(str) == cust["std"]]
    if key == "type":
        return res[res["Тип_изделия"].fillna("").astype(str).str.upper() == cust["type"].upper()]
    if key == "joint":
        if "Исполнение" not in res.columns:
            return res
        pair_strs = {f"{a}-{b}" for a, b in core.JOINT_EXEC_PAIRS.get(cust["joint"], set())}
        return res[res["Исполнение"].fillna("").astype(str).isin(pair_strs)]
    if key == "dims":
        return res[dims_mask(res, cust["dims_req"])]
    if key == "dn":
        if "DN_num" not in res.columns:
            return res
        return res[pd.to_numeric(res["DN_num"], errors="coerce") == cust["dn"]]
    if key == "pn":
        if cust.get("pn_lo") is not None and cust.get("pn_hi") is not None:
            return res[pressure_range_mask(res, cust["pn_lo"], cust["pn_hi"])]
        return res[pressure_mask(res, cust["pn"])]
    if key == "class":
        kgs_lo = core.ANSI_CLASS_TO_KGS.get(cust["class_lo"])
        kgs_hi = core.ANSI_CLASS_TO_KGS.get(cust.get("class_hi") or cust["class_lo"])
        if kgs_lo is None or kgs_hi is None:
            return res
        return res[pressure_range_mask(res, kgs_lo, kgs_hi)]
    if key == "thickness":
        th = pd.to_numeric(res["Толщина / Высота"], errors="coerce")
        return res[(th - cust["thickness"]).abs() < 1e-9]
    if key == "steel":
        return res[res["Материал"].fillna("").astype(str).map(core.mat_norm)
                  == core.mat_norm(cust["steel"])]
    return res


def _miss_hint(key, prev, cust):
    if key == "pn":
        # один DN у ГОСТ/ОСТ может закрываться НЕСКОЛЬКИМИ диапазонами давления
        # (разный диаметр фланца под низкое/высокое PN) — берём диапазон,
        # ближайший к запрошенному давлению, а не объединение всех сразу.
        # итерация 50: row_pressure_kgs — тот же источник, что у фильтра
        # (кольца: текст «PN / Давление» в МПа)
        rng = [row_pressure_kgs(r)[:2] for _, r in prev.iterrows()]
        pairs = sorted({(a, b) for a, b in rng if a is not None and b is not None})
        if pairs:
            if cust.get("pn_lo") is not None and cust.get("pn_hi") is not None:
                target = (cust["pn_lo"] + cust["pn_hi"]) / 2.0
                req_txt = f"{cust['pn_lo']:.1f}–{cust['pn_hi']:.1f}"
            else:
                target = cust["pn"]
                req_txt = f"{target:.1f}"
            def _dist(p):
                a, b = p
                return 0.0 if a <= target <= b else min(abs(target - a), abs(target - b))
            lo_v, hi_v = min(pairs, key=_dist)
            return f"строки дают {lo_v:.1f}–{hi_v:.1f} кгс (запрошено {req_txt})"
        return "у оставшихся строк давление не указано в прайсе"
    if key == "steel":
        mats = sorted(set(prev["Материал"].dropna().astype(str)) - {""})
        return ("доступные материалы: " + ", ".join(mats[:10])) if mats else "доступных материалов нет"
    if key == "thickness":
        th = pd.to_numeric(prev["Толщина / Высота"], errors="coerce").dropna()
        vals = sorted(set(th.tolist()))
        return ("доступные толщины: " + ", ".join(str(v) for v in vals[:10])) \
            if vals else "толщина не указана ни у одной строки"
    if key == "dn":
        if "DN_num" not in prev.columns:
            return "DN не определён у оставшихся строк"
        dns = sorted(set(pd.to_numeric(prev["DN_num"], errors="coerce")
                         .dropna().astype(int).tolist()))
        target = cust.get("dn")
        near = sorted(dns, key=lambda d: abs(d - target))[:3] if dns and target is not None else []
        return ("ближайшие DN: " + ", ".join(str(d) for d in near)) if near else "строк с DN нет"
    if key == "dims":
        # для каждого несовпавшего ключа dims_req — ближайшее значение среди
        # оставшихся строк (не .map() на str-колонке — цикл по значениям,
        # как в dims_mask/_dims_hit).
        req = cust.get("dims_req") or {}
        parts = []
        for k in sorted(req):
            vals = [d[k] for d in (core.norm_dims(v) for v in prev["Основные_размеры"]) if k in d]
            if not vals:
                parts.append(f"{k}: нет данных у оставшихся строк")
                continue
            nearest = min(vals, key=lambda v: abs(v - req[k]))
            if abs(nearest - req[k]) > core.CROSS_TOL:
                parts.append(f"{k}: запрошено {req[k]:.1f}, ближайшее {nearest:.1f}")
        return "; ".join(parts) if parts else "размеры не совпали ни по одному ключу"
    if key == "class":
        classes = sorted(set(prev["PN / Давление"].dropna().astype(str)) - {""})
        return ("доступные классы: " + ", ".join(classes[:10])) if classes \
            else "класс давления не задан ни у одной строки"
    return ""


def diagnose_miss(df, cust) -> str:
    """Пошагово прогоняет фильтры resolve_customer в порядке [маркировка,
    стандарт, тип, размеры(ANSI)/DN, давление/класс давления(ANSI), толщина,
    сталь] и возвращает описание первого фильтра, обнуливший подбор, с
    подсказкой (диапазон давления/список материалов/список толщин/ближайшие
    DN/несовпавшие размеры/доступные классы — итерация 22). Пустая строка,
    если при тех же условиях подбор не пуст."""
    res = df
    for key, label in _MISS_STEPS:
        if not _miss_step_applies(cust, key):
            continue
        prev = res
        res = _miss_apply_step(res, cust, key)
        if not len(res):
            hint = _miss_hint(key, prev, cust)
            return f"фильтр «{label}» обнулил подбор: {hint}" if hint \
                else f"фильтр «{label}» обнулил подбор"
    return ""


def _unify(s) -> str:
    """Нормализация для поиска: регистр, неразрывный пробел, кириллическая х -> x."""
    return (str(s).lower()
            .replace("\u00a0", " ")
            .replace("х", "x").replace("Х", "x"))


def _syn_mask(df, token):
    """Доп. условие токена из core.SYN_TOKENS (R4, итерация 12) — токены вроде
    «набивка»/«ф-4» подменяются структурным условием по колонке (категория/
    материал), а не только буквальным вхождением в текст: «набивка» (ед.ч.)
    не входит подстрокой в «Набивки» (категория), «ф-4»/«фторопласт» —
    обиходные синонимы категории «ПТФЭ», а не конкретного кода материала."""
    syn = core.SYN_TOKENS.get(token)
    if not syn:
        return None
    col, val = syn
    if col not in df.columns:
        return None
    if isinstance(val, (list, tuple, set)):
        return df[col].isin(val)
    return df[col] == val


# ---- свёртка кода для текстового поиска (итерация 50, находка 8) ----
# «МГ140», «МГ 140», «МГ-140» (и «МС500»/«МС 500», «КГФГ»/«КГФ-Г») — один код.
# Свёртка: UPPER, кириллическая Х -> X, омоглифы core._CYR2LAT_BRAND (МС=MC,
# как _nv_fold), разделитель пробел/дефис МЕЖДУ буквой и следующей буквой или
# цифрой кода убирается (между буквами — только дефис: слова через пробел не
# склеиваются, иначе теряется левая граница кода). Совпадение по свёртке —
# только ДОПОЛНИТЕЛЬНО к прежнему подстрочному совпадению (выдача может только
# расшириться); код матчится с левой границы (не с середины «МС500» для «С-500»).
_CODE_SEP_RE = re.compile(r"(?<=[A-ZА-ЯЁ])(?:[\s\-]+(?=\d)|-+(?=[A-ZА-ЯЁ]))")
_CODE_LEFT = r"(?<![A-ZА-ЯЁ0-9])"
_HAY_CACHE: dict = {}


def _code_fold(s) -> str:
    up = str(s).upper().replace(" ", " ").replace("Х", "X")
    up = "".join(core._CYR2LAT_BRAND.get(ch, ch) for ch in up)
    return _CODE_SEP_RE.sub("", up)


def _code_fold_series(s: pd.Series) -> pd.Series:
    up = (s.fillna("").astype(str).str.upper().str.replace(" ", " ", regex=False)
          .str.replace("Х", "X", regex=False))
    for k, v in core._CYR2LAT_BRAND.items():
        up = up.str.replace(k, v, regex=False)
    return up.str.replace(_CODE_SEP_RE.pattern, "", regex=True)


def _hay_frame(df) -> pd.DataFrame:
    """hay/ns (как раньше в search_generic) + code (свёртка кода) — кэш для
    полного каталога (id+len, один слот, как _dims_frame)."""
    key = (id(df), len(df))
    if _HAY_CACHE.get("key") == key:
        return _HAY_CACHE["frame"]
    # fillna("") перед astype(str): начиная с pandas 3.0 astype(str) сохраняет
    # NaN как NA (а не как строку "nan"), и "+" на NA даёт NA на всю строку —
    # строка с любым пустым полем выпадала бы из поиска целиком.
    raw = (df["Маркировка_в_прайсе"].fillna("").astype(str) + " | "
           + df["SKU"].fillna("").astype(str) + " | "
           + df["Материал"].fillna("").astype(str) + " | "
           + df["Категория"].fillna("").astype(str))
    hay = raw.map(_unify)
    # без пробелов: токен «мс101» обязан матчить «МС 101 24х24» — сверяем и
    # с исходным hay, и с его версией без пробелов (маркировка в прайсе не
    # всегда пишется слитно, а SKU — всегда).
    frame = pd.DataFrame({"hay": hay, "ns": hay.str.replace(" ", "", regex=False),
                          "code": _code_fold_series(raw)}, index=df.index)
    if len(df) > 1000:
        _HAY_CACHE.clear()
        _HAY_CACHE.update(key=key, frame=frame)
    return frame


def _token_hit(df, hf, t) -> pd.Series:
    m = hf["hay"].str.contains(t, regex=False) | hf["ns"].str.contains(t, regex=False)
    if re.search(r"[^\W\d_]", t):
        m = m | hf["code"].str.contains(_CODE_LEFT + re.escape(_code_fold(t)), regex=True)
    syn = _syn_mask(df, t)
    return m if syn is None else (m | syn)


def _search_tokens(text, hf) -> list:
    """Токены поиска: нарезка по пробелам/«,;», токены короче 2 символов
    отбрасываются (как раньше); короткий буквенный токен (1–4 буквы) +
    следующий токен с цифры склеиваются в код («МГ 140» -> «мг140»), если
    такой код есть в каталоге (иначе «лист 1000» остался бы без совпадений)."""
    raw = [t for t in re.split(r"[\s,;]+", _unify(text)) if t]
    out, i = [], 0
    while i < len(raw):
        t = raw[i]
        if (re.fullmatch(r"[^\W\d_]{1,4}", t) and i + 1 < len(raw) and raw[i + 1][:1].isdigit()
                and hf["code"].str.contains(_CODE_LEFT + re.escape(_code_fold(t + raw[i + 1])),
                                            regex=True).any()):
            out.append(t + raw[i + 1])
            i += 2
            continue
        if len(t) >= 2:
            out.append(t)
        i += 1
    return out


# ---- кольца ASME по R-номеру (итерация 50b) ----
# «Прокладка восьмиугольная R45 CL1500», «Oval R16», «RTJ R-45»: R-номер +
# признак кольца -> категории «Овальные»/«Восьмиугольные», колонка «DN / Размер»
# = R<номер>. Класс давления (CL/#) кольца не фильтрует (у колец ASME давления
# в каталоге нет), сталь из строки сужает выдачу.
_RING_R_RE = re.compile(r"(?<![A-Za-z0-9])R\s*-?\s*(\d{1,3})(?![\dA-Za-z])")
_RING_OVAL_RE = re.compile(r"ОВАЛЬН|OVAL", re.IGNORECASE)
_RING_OCT_RE = re.compile(r"ВОСЬМИУГОЛЬН|OCTAGON", re.IGNORECASE)
_RING_RTJ_RE = re.compile(r"(?<![A-Za-z])RTJ(?![A-Za-z])", re.IGNORECASE)


def ring_request(line):
    """None или (R-номер, категории колец) для строки заявки на кольцо ASME."""
    text, _qty = normalize_request_line(line)
    if "СНП" in text.upper():
        return None
    m = _RING_R_RE.search(text)
    if not m:
        return None
    cats = []
    if _RING_OVAL_RE.search(text):
        cats.append("Овальные")
    if _RING_OCT_RE.search(text):
        cats.append("Восьмиугольные")
    if not cats:
        if not _RING_RTJ_RE.search(text):
            return None
        cats = ["Овальные", "Восьмиугольные"]
    return m.group(1), cats


def search_rings(df, line, cust=None):
    rr = ring_request(line)
    if rr is None:
        return df.iloc[0:0]
    num, cats = rr
    cust = cust if cust is not None else parse_customer_line(line)
    size = df["DN / Размер"].fillna("").astype(str).str.upper().str.replace(r"\s+", "", regex=True)
    res = df[df["Категория"].isin(cats) & (size == f"R{int(num)}")]
    if cust.get("steel") and len(res):
        res = res[res["Материал"].fillna("").astype(str).map(core.mat_norm)
                  == core.mat_norm(cust["steel"])]
    return sort_for_manager(res)


def search_generic(df, query):
    # итерация 50: количество/единицы — не токены поиска (normalize_request_line)
    if ring_request(query) is not None:
        return search_rings(df, query)
    text, _qty = normalize_request_line(query)
    hf = _hay_frame(df)
    tokens = _search_tokens(text, hf)
    if not tokens:
        return df.iloc[0:0]
    mask = _token_hit(df, hf, tokens[0])
    for t in tokens[1:]:
        mask &= _token_hit(df, hf, t)
    return df[mask]


def _remaining_tokens(line, cust, df=None) -> list:
    """Токены строки (нарезка search_generic) БЕЗ тех, что уже потребили
    структурные критерии search_with_criteria: pn.../dn.../ду... (если
    давление/DN распознаны), коды стали (если сталь распознана), толщина с
    «мм» (если толщина распознана), «N шт» (количество — не критерий и не
    искомое слово в любом случае), фрагмент маркировки (если он всё же
    есть — в этой ветке маршрутизации обычно нет). Маскирует теми же
    регэкспами, что parse_customer_line извлекал сами значения — гарантия,
    что извлечённое и вычеркнутое из токенов — одно и то же."""
    work, _qty = normalize_request_line(line)
    if cust.get("dn") is not None:
        work = re.sub(r"\bDN\s*\d+\b", " ", work, flags=re.IGNORECASE)
        work = re.sub(r"\b(?:ДУ|DU)\s*\d+\b", " ", work, flags=re.IGNORECASE)
    if cust.get("pn") is not None or cust.get("pn_lo") is not None:
        work = re.sub(r"\bPN\s*[\d.,]+\s*(?:МПА|MPA|КГС|KGF)?\b", " ", work, flags=re.IGNORECASE)
    if cust.get("steel"):
        work = STEEL_RE.sub(" ", work)
        work = BARE_STEEL_RE.sub(" ", work)
    if cust.get("thickness") is not None:
        work = re.sub(r"толщ[.:]?\s*[\d.,]+|[-\s][\d.,]+\s*мм", " ", work, flags=re.IGNORECASE)
    if cust.get("std"):
        # итерация 20: std потреблён как ФИЛЬТР в search_with_criteria (как
        # dn/pn/steel/thickness выше) — токен «ост»/«гост»/«asme» больше не
        # должен требоваться буквально в hay (там его как раз и не было бы
        # для строк без явной колонки-дубля стандарта в маркировке).
        work = re.sub(r"\b" + re.escape(cust["std"]) + r"\b", " ", work, flags=re.IGNORECASE)
    if cust.get("mark"):
        work = work.replace(cust["mark"], " ")
    if df is not None:
        return _search_tokens(work, _hay_frame(df))
    return [t for t in re.split(r"[\s,;]+", _unify(work)) if len(t) >= 2]


def search_with_criteria(df, line, cust):
    """Search-путь с учётом распознанных критериев (итерация 18, закрытие
    топ-причины промахов аудита боевого журнала — "search: токен «pn16»"):
    строка без маркировки/типа/«СНП» (routed_resolve=False), но с
    распознанными DN/PN/сталью/толщиной — раньше уходила в чистый
    search_generic, чей hay не содержит ни давления, ни DN, поэтому токены
    "pn16"/"dn250" никогда не находились, хотя позиция в каталоге есть.

    Применяет критерии КАК ФИЛЬТРЫ (не привязано к категории — иначе это
    был бы resolve_customer; каждый фильтр — только если задан в cust),
    затем — токены search_generic по ОСТАВШИМСЯ словам строки (критериальные
    токены исключены _remaining_tokens). Пустой остаток токенов после
    фильтров — не промах: это ожидаемый случай "вся заявка — это критерии"
    (пример: "DN250 PN16 2" — после вычитания DN/PN/qty токенов не остаётся).
    Возвращает DataFrame, отсортированный sort_for_manager; если критерии
    обнулили выборку — пустой DataFrame (диагностика — забота вызывающего,
    через diagnose_miss, те же поля)."""
    if ring_request(line) is not None:
        return search_rings(df, line, cust)
    res = df
    if cust.get("std"):
        # итерация 20: std — тоже ФИЛЬТР (как в resolve_customer), не токен
        # для поиска подстрокой; закрывает промах «DN250 PN16 ГОСТ» — раньше
        # std не потреблялся нигде в этой ветке и просто выпадал как
        # неучтённый «лишний» токен, не влияя на выборку (ОСТ-строки не
        # отсеивались), хотя менеджер явно указал стандарт.
        res = res[res["Стандарт"].fillna("").astype(str) == cust["std"]]
    if cust.get("dn") is not None and "DN_num" in res.columns:
        res = res[pd.to_numeric(res["DN_num"], errors="coerce") == cust["dn"]]
    if cust.get("pn_lo") is not None and cust.get("pn_hi") is not None:
        res = res[pressure_range_mask(res, cust["pn_lo"], cust["pn_hi"])]
    elif cust.get("pn") is not None:
        res = res[pressure_mask(res, cust["pn"])]
    if cust.get("thickness") is not None:
        th = pd.to_numeric(res["Толщина / Высота"], errors="coerce")
        res = res[(th - cust["thickness"]).abs() < 1e-9]
    if cust.get("steel"):
        res = res[res["Материал"].fillna("").astype(str).map(core.mat_norm)
                  == core.mat_norm(cust["steel"])]
    if not len(res):
        return res

    tokens = _remaining_tokens(line, cust, df)
    if not tokens:
        return sort_for_manager(res)

    hf = _hay_frame(df).loc[res.index] if len(df) > 1000 else _hay_frame(res)
    mask = _token_hit(res, hf, tokens[0])
    for t in tokens[1:]:
        mask &= _token_hit(res, hf, t)
    return sort_for_manager(res[mask])


def diagnose_search(df, line) -> str:
    """Диагностика промаха search_generic (R2, итерация 12): какие токены
    заявки не встречаются в каталоге ни в каком виде (с учётом core.SYN_TOKENS)
    и топ-3 ближайших кандидата по числу совпавших токенов. В отличие от
    diagnose_miss (зеркалит структурные фильтры resolve_customer), работает
    на том же токенном поиске, что и search_generic — для промахов текстового
    поиска, не привязанного к маркировке СНП."""
    if df is None or not len(df):
        return ""
    text, _qty = normalize_request_line(line)
    hf = _hay_frame(df)
    tokens = _search_tokens(text, hf)
    if not tokens:
        return ""

    missing = []
    hit_counts = pd.Series(0, index=df.index)
    for t in tokens:
        m = _token_hit(df, hf, t)
        if not m.any():
            missing.append(t)
        hit_counts = hit_counts + m.astype(int)

    parts = []
    if missing:
        parts.append("не найдены токены: " + ", ".join(missing))
    top_idx = hit_counts[hit_counts > 0].sort_values(ascending=False).head(3).index
    if len(top_idx):
        cand = df.loc[top_idx, "Маркировка_в_прайсе"].astype(str).tolist()
        parts.append("возможные кандидаты: " + "; ".join(cand))
    return "; ".join(parts)


# ============================ БЛИЖАЙШИЕ ВАРИАНТЫ ПРИ ПРОМАХЕ ============================
# Итерация 41 (SELECTION_SPEC §18): точный подбор пуст -> не «кандидаты по слову
# «набивка»», а честная пометка + варианты по СЕМЕЙСТВУ кода. Вызывается только
# после пустого точного подбора; resolve_customer/search_generic не меняются.

_NV_LET = r"[A-ZА-ЯЁ]"
# семейный ключ (guard): >=2 букв СРАЗУ за которыми (через необязательный
# пробел/дефис) идёт цифра — голые «Н»/«С» семью не образуют
_NV_FAMILY_RE = re.compile(r"(?<![A-ZА-ЯЁ0-9])(" + _NV_LET + r"{2,})[\s\-]?(\d+)")
# ключ аналога из справочника поставщика — точный код, префикс от 1 буквы
_NV_ANALOG_RE = re.compile(r"(?<![A-ZА-ЯЁ0-9])(" + _NV_LET + r"+)[\s\-]?(\d+)")
_NV_WORDS_RE = re.compile(r"НАБИВКА|ЛЕНТА|САЛЬНИКОВАЯ")
_NV_SEC_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*X\s*(\d+(?:[.,]\d+)?)")
_NV_STRIP_RE = re.compile(r"[\s\-]+")


def _nv_fold(s) -> str:
    """UPPER, без слов НАБИВКА/ЛЕНТА/САЛЬНИКОВАЯ, кириллическая Х -> X,
    омоглифы МС/MC, Н/H, С/C -> латиница (core._CYR2LAT_BRAND)."""
    up = _NV_WORDS_RE.sub(" ", str(s).upper().replace(" ", " ").replace("Х", "X"))
    return "".join(core._CYR2LAT_BRAND.get(ch, ch) for ch in up)


def _nv_fold_series(s: pd.Series) -> pd.Series:
    """Векторный _nv_fold для колонки каталога (без .map()/.apply())."""
    up = (s.fillna("").astype(str).str.upper().str.replace(" ", " ", regex=False)
           .str.replace("Х", "X", regex=False)
           .str.replace(_NV_WORDS_RE.pattern, " ", regex=True))
    for k, v in core._CYR2LAT_BRAND.items():
        up = up.str.replace(k, v, regex=False)
    return up


def _nv_keys(folded: pd.Series, pattern) -> pd.DataFrame:
    """Все пары (префикс, число) колонки -> DataFrame [idx, pre, num]."""
    ex = folded.str.extractall(pattern.pattern)
    if ex.empty:
        return pd.DataFrame({"idx": [], "pre": [], "num": []})
    ex = ex.reset_index()
    ex.columns = ["idx", "match", "pre", "num"]
    return ex[["idx", "pre", "num"]]


def _nv_ref_index(rk) -> dict:
    """{код без пробелов/дефисов (свёрнутый): item} справочника RK."""
    return {_NV_STRIP_RE.sub("", _nv_fold(it.get("code", ""))): it
            for it in (rk or {}).get("items", [])}


def _nv_request_code(line, ref, cat_norm):
    """Код семьи из заявки: (префикс, число, склеенный код или None, item или None)
    либо None. «RK-240 I» склеивается с одиночной буквой-суффиксом, только если
    склеенный код есть в справочнике или каталоге (cat_norm — марки каталога без
    пробелов/дефисов); без пробела допускается суффикс до 3 букв."""
    up = _nv_fold(line)
    m = _NV_FAMILY_RE.search(up)
    if not m:
        return None
    pre, num = m.group(1), m.group(2)
    rest = up[m.end():]
    base = pre + num
    glued = None
    for sm in (re.match(r"^\s+([A-Z])(?![A-Z0-9])", rest),
               re.match(r"^\-?([A-Z]{1,3})(?![A-Z0-9])", rest)):
        if not sm:
            continue
        cand = base + sm.group(1)
        if cand in ref or cat_norm.str.contains(cand, regex=False).any():
            glued = cand
            break
    return pre, num, glued, ref.get(glued or base)


def _nv_section(text):
    m = _NV_SEC_RE.search(_nv_fold(text))
    if not m:
        return None
    return float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", "."))


def _nv_empty(df) -> pd.DataFrame:
    return df.iloc[0:0].assign(Причина=pd.Series(dtype=object),
                               Уровень=pd.Series(dtype=object))


def nearest_variants(df, line, cust=None, limit=30, rk=None) -> pd.DataFrame:
    """Ближайшие варианты при пустом точном подборе (SELECTION_SPEC §18).
    Колонки каталога + «Причина» + «Уровень»: 'a' — аналог по данным
    поставщика (справочник RK), 'b' — то же семейство бренда, 'c' — слабое
    совпадение (топ-3 diagnose_search-подобный). Строки не дублируются между
    уровнями; вызывается только когда точный подбор пуст; df не меняется."""
    if df is None or not len(df):
        return _nv_empty(df if df is not None else pd.DataFrame(columns=["Категория"]))
    line, _qty = normalize_request_line(line)   # итерация 50: без «N шт»
    rr = ring_request(line)
    if rr is not None:   # итерация 50b: у R-номера кольца — ближайшие только среди колец
        df = df[df["Категория"].isin(core.PRESSURE_TEXT_MPA_CATEGORIES)]
        if not len(df):
            return _nv_empty(df)
    rk = core.load_rk_catalog() if rk is None else rk
    ref = _nv_ref_index(rk)
    fold = _nv_fold_series(df["Маркировка_в_прайсе"])
    cat_norm = fold.str.replace(_NV_STRIP_RE.pattern, "", regex=True)
    req = _nv_request_code(line, ref, cat_norm)
    if not req:
        return _nv_weak(df, line)
    pre, num, glued, item = req
    req_disp = item["code"] if item else (glued or pre + num)
    stock = pd.to_numeric(df.get("Остаток"), errors="coerce")
    price = pd.to_numeric(df.get("Цена_без_НДС"), errors="coerce")
    sec_req = _nv_section(line)
    secs_all = fold.str.extract(_NV_SEC_RE.pattern)
    parts = []
    used = set()

    # ---- уровень (a): аналоги кода по данным поставщика ----
    if item:
        conflict = " (сайт противоречив)" if "конфликт" in str(item.get("note", "")) else ""
        seen_an: dict = {}
        for src, lst in (("таблица", item.get("analogs_table") or []),
                         ("карточка", item.get("analogs_card") or [])):
            for a in lst:
                seen_an.setdefault(a, []).append(src)
        keys = _nv_keys(fold, _NV_ANALOG_RE)
        rows_a = []
        for a, srcs in seen_an.items():
            am = _NV_ANALOG_RE.search(_nv_fold(a))
            if not am or keys.empty:
                continue
            hit = keys[(keys["pre"] == am.group(1)) & (keys["num"] == am.group(2))]["idx"].unique()
            for ix in hit:
                if ix in used:
                    continue
                used.add(ix)
                rows_a.append((ix, f"аналог по данным поставщика ({'+'.join(srcs)} vendor_r): "
                                   f"{item['code']} ≈ {a}{conflict}"))
        if rows_a:
            a_df = df.loc[[r[0] for r in rows_a]].copy()
            a_df["Причина"] = [r[1] for r in rows_a]
            a_df["Уровень"] = "a"
            n_al = pd.to_numeric(secs_all[0].str.replace(",", ".", regex=False), errors="coerce")
            a_df = a_df.assign(
                _o=range(len(a_df)),
                _ds=((n_al.loc[a_df.index] - sec_req[0]).abs() if sec_req
                     else pd.Series(0.0, index=a_df.index)),
                _ns=~(stock.loc[a_df.index] > 0), _p=price.loc[a_df.index])
            parts.append(a_df.sort_values(["_ds", "_o", "_ns", "_p"], kind="mergesort",
                                          na_position="last")
                         .drop(columns=["_o", "_ds", "_ns", "_p"]))

    # ---- уровень (b): то же семейство бренда (префикс >=2 букв + цифра) ----
    low = _unify(line)
    toks = [t for t in re.split(r"[\s,;]+", low) if len(t) >= 2]
    cmask = None
    if "набивка" in toks:
        cmask = _syn_mask(df, "набивка")
    elif "лента" in toks:
        cmask = df["Категория"] == "Ленты"
    elif item and item.get("catalog_category"):
        cmask = df["Категория"] == item["catalog_category"]
    fam = _nv_keys(fold, _NV_FAMILY_RE)
    fam = fam[fam["pre"] == pre].drop_duplicates("idx")
    fam = fam[~fam["idx"].isin(used)]
    if cmask is not None:
        fam = fam[fam["idx"].isin(df.index[cmask])]
    if not fam.empty:
        secs = secs_all
        n1 = pd.to_numeric(secs[0].str.replace(",", ".", regex=False), errors="coerce")
        fam = fam.set_index("idx")
        b_df = df.loc[fam.index].copy()
        b_df["_dc"] = (pd.to_numeric(fam["num"], errors="coerce") - int(num)).abs()
        b_df["_ds"] = ((n1.loc[b_df.index] - sec_req[0]).abs() if sec_req
                       else pd.Series(0.0, index=b_df.index))
        b_df["_ns"] = ~(stock.loc[b_df.index] > 0)
        b_df["_p"] = price.loc[b_df.index]
        b_df = b_df.sort_values(["_dc", "_ds", "_ns", "_p"], kind="mergesort",
                                na_position="last")
        reasons = []
        for ix in b_df.index:
            txt = (f"ближайший вариант: семейство {pre}, код {pre}-{fam.at[ix, 'num']}"
                   f" (запрошен {req_disp})")
            if sec_req and pd.notna(secs.at[ix, 0]):
                txt += f", сечение {secs.at[ix, 0]}х{secs.at[ix, 1]} (запрошено {sec_req[0]:g}х{sec_req[1]:g})"
            reasons.append(txt)
        b_df["Причина"] = reasons
        b_df["Уровень"] = "b"
        parts.append(b_df.drop(columns=["_dc", "_ds", "_ns", "_p"]))

    # отчёт справочника: код есть у поставщика, позиции в каталоге нет
    note = ""
    if item and not cat_norm.str.contains(_NV_STRIP_RE.sub("", _nv_fold(item["code"])),
                                          regex=False).any():
        note = (f"{item['code']} есть у Vendor R "
                f"({item.get('group', '')}: {item.get('description', '')}), "
                f"в каталоге отсутствует")
    if parts:
        # лимит режет хвост аналогов (a), а не семейство (b): семейство —
        # ядро ответа для кода без строки в каталоге, его не вытесняем
        n_b = len(parts[-1]) if parts[-1]["Уровень"].iloc[0] == "b" else 0
        if len(parts) == 2:
            parts[0] = parts[0].head(max(limit - n_b, limit // 2))
        out = pd.concat(parts).head(limit)
    else:
        out = _nv_weak(df, line)
    if len(out) and note:
        out = out.copy()
        out.iloc[0, out.columns.get_loc("Причина")] = note + "; " + str(out.iloc[0]["Причина"])
    return out


def _nv_weak(df, line) -> pd.DataFrame:
    """Уровень (c): топ-3 по числу совпавших токенов (как diagnose_search),
    с честной причиной."""
    text, _qty = normalize_request_line(line)
    hf = _hay_frame(df)
    tokens = _search_tokens(text, hf)
    if not tokens:
        return _nv_empty(df)
    cnt = pd.Series(0, index=df.index)
    for t in tokens:
        cnt = cnt + _token_hit(df, hf, t).astype(int)
    top = cnt[cnt > 0].sort_values(ascending=False, kind="mergesort").head(3).index
    out = df.loc[top].copy()
    out["Причина"] = "слабое совпадение: только общие слова"
    out["Уровень"] = "c"
    return out


# ============================ АУДИТ БОЕВОГО ЖУРНАЛА ============================
# Итерация 17: entry.py audit прогоняет строки request_history.json через тот
# же маршрут, что gui.OrderTab.on_resolve/entry.cmd_search (parse -> route ->
# resolve/search), и агрегирует исход — чистая функция, без файлового I/O
# (чтение журнала — забота entry.py audit).

_MISS_FILTER_RE = re.compile(r"фильтр «([^»]+)»")
_MISS_TOKENS_RE = re.compile(r"не найдены токены: ([^;]+)")


def audit_lines(lines, df):
    """Классифицирует каждую строку lines: точное попадание / промах resolve
    (причина — первый обнуливший фильтр diagnose_miss) / промах search
    (причина — по одному счётчику на каждый ненайденный токен diagnose_search)
    / «не распознано» (has_criteria=False при routed_resolve). Возвращает
    (summary, details):
      summary — {total, hits, misses, hit_rate, top_reasons (до 5,
                 [(причина, счёт)]), top_anomalies (до 5, [(строка, описание)])}
      details — по записи на строку: {line, cust, category, rows?, reason?, anomaly?}

    Аномалии разбора — независимо от исхода hit/miss: «СНП» в тексте, но
    mark/type не распознаны; «DN»/«ДУ» в тексте, но dn не распознан — сигнал
    возможного пробела в parse_customer_line, не факт промаха подбора."""
    details = []
    reason_counts: dict[str, int] = {}
    anomalies = []
    hits = 0
    nearest_misses = 0
    for raw_line in lines:
        line = str(raw_line)
        cust = parse_customer_line(line)
        routed_resolve = bool(cust.get("mark") or cust.get("type") or "СНП" in line.upper())
        no_criteria = routed_resolve and not has_criteria(cust)
        use_criteria_search = not routed_resolve and has_dn_pn_steel_thickness(cust)
        rec = {"line": line, "cust": cust}

        if no_criteria:
            rec["category"] = "unrecognized"
            rec["reason"] = "не распознано (параметры отсутствуют)"
            reason_counts[rec["reason"]] = reason_counts.get(rec["reason"], 0) + 1
        elif routed_resolve:
            # тот же маршрут, что gui.OrderTab.on_resolve/entry.cmd_search:
            # guard по "Категория" — БЕЗ доп. условия на mark/type, category
            # фильтр по СНП применяется всегда, когда сработал routed_resolve.
            res = resolve_customer(df, cust)
            if len(res) and "Категория" in res.columns:
                res = res[res["Категория"].fillna("").astype(str) == "СНП"]
            elif len(res):
                res = res.iloc[0:0]
            if len(res):
                rec["category"] = "hit"
                rec["rows"] = int(len(res))
                hits += 1
            else:
                rec["category"] = "miss_resolve"
                diag = diagnose_miss(df, cust)
                m = _MISS_FILTER_RE.search(diag)
                reason = f"resolve: {m.group(1)}" if m else "resolve: причина не определена"
                rec["reason"] = reason
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        elif use_criteria_search:
            # итерация 18: строка без mark/type/«СНП», но с DN/PN/сталью/
            # толщиной — тот же маршрут, что gui.on_resolve/entry.cmd_search
            # (SELECTION_SPEC A12); промах диагностируется через diagnose_miss
            # (те же структурные поля, что и у resolve-ветки), не diagnose_search.
            res = search_with_criteria(df, line, cust)
            if len(res):
                rec["category"] = "hit"
                rec["rows"] = int(len(res))
                hits += 1
            else:
                rec["category"] = "miss_resolve"
                diag = diagnose_miss(df, cust)
                m = _MISS_FILTER_RE.search(diag)
                reason = f"resolve: {m.group(1)}" if m else "resolve: причина не определена"
                rec["reason"] = reason
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        else:
            res = search_generic(df, line)
            if len(res):
                rec["category"] = "hit"
                rec["rows"] = int(len(res))
                hits += 1
            else:
                rec["category"] = "miss_search"
                # итерация 41: промах остаётся промахом (hit-rate не меняется),
                # но считаем, для скольких есть ближайшие варианты
                if len(nearest_variants(df, line, cust)):
                    rec["nearest"] = True
                    nearest_misses += 1
                diag = diagnose_search(df, line)
                m = _MISS_TOKENS_RE.search(diag)
                tokens = [t.strip() for t in m.group(1).split(",")] if m else []
                if tokens:
                    for t in tokens:
                        reason = f"search: токен «{t}»"
                        reason_counts[reason] = reason_counts.get(reason, 0) + 1
                    rec["reason"] = f"search: не найдены токены {', '.join(tokens)}"
                else:
                    rec["reason"] = "search: причина не определена"
                    reason_counts[rec["reason"]] = reason_counts.get(rec["reason"], 0) + 1

        anomaly = None
        if "СНП" in line.upper() and not cust.get("mark") and not cust.get("type"):
            anomaly = "«СНП» в строке, но mark/type не распознаны"
        elif re.search(r"\bDN(?=\d|\s|$)|\bДУ(?=\d|\s|$)", line.upper()) and cust.get("dn") is None:
            anomaly = "DN/ДУ в строке, но dn не распознан"
        if anomaly:
            rec["anomaly"] = anomaly
            if len(anomalies) < 5:
                anomalies.append((line, anomaly))

        details.append(rec)

    total = len(details)
    top_reasons = sorted(reason_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
    summary = {
        "total": total,
        "hits": hits,
        "misses": total - hits,
        "hit_rate": (hits / total) if total else 0.0,
        "top_reasons": top_reasons,
        "top_anomalies": anomalies,
        "misses_with_nearest": nearest_misses,
    }
    return summary, details


# ============================ ПОДБОР АНАЛОГОВ ============================
# Перенесено из gui.py (итерация 4): та же логика, справочники core те же.

def find_smart_analogs(df: pd.DataFrame, cust: dict):
    """Каскад: сначала без сталь/толщина, затем — если пусто — без стандарта.
    Апгрейд стали (tier не ниже запрошенного), допуск по толщине ±1.3 мм."""
    relaxed = dict(cust, steel=None, thickness=None)
    cand = resolve_customer(df, relaxed)
    if cust.get("mark") or cust.get("type"):
        cand = cand[cand["Категория"].fillna("").astype(str) == "СНП"]
    extra_note = ""
    joint_swap = False
    if not len(cand) and cust.get("joint"):
        # итерация 26 (SELECTION_SPEC, «Стыки исполнений»): тип клиента
        # релаксируется ТОЛЬКО внутри семьи одного стыка (core.JOINT_TYPE_
        # FAMILY) — тот же стандарт, тот же joint-фильтр на «Исполнение»
        # (relaxed_fam НЕ сбрасывает cust["joint"]), но type заменён на
        # isin(семья) вместо точного равенства. Шаг идёт ДО падения std
        # ниже — релаксация типа внутри одного стандарта консервативнее
        # смены стандарта целиком.
        family = core.JOINT_TYPE_FAMILY.get(cust["joint"])
        if family:
            relaxed_fam = dict(relaxed, type=None)
            cand_fam = resolve_customer(df, relaxed_fam)
            cand_fam = cand_fam[cand_fam["Категория"].fillna("").astype(str) == "СНП"]
            cand_fam = cand_fam[cand_fam["Тип_изделия"].fillna("").astype(str).isin(family)]
            if len(cand_fam):
                cand = cand_fam
                joint_swap = True
                extra_note = f"совместимый тип исполнения (стык {_joint_label(cust['joint'])})"
    if not len(cand) and relaxed.get("std"):
        relaxed2 = dict(relaxed, std=None)
        cand = resolve_customer(df, relaxed2)
        if cust.get("mark") or cust.get("type"):
            cand = cand[cand["Категория"].fillna("").astype(str) == "СНП"]
        extra_note = "аналог по другому стандарту — сверьте исполнения фланцев"
    if not len(cand):
        return pd.DataFrame(), f"DN{cust.get('dn')}: допустимых аналогов не найдено"

    res = cand.copy()
    req_mat = cust.get("steel")
    req_tier = core.mat_tier(req_mat) if req_mat else None
    res["_tier"] = res["Материал"].map(core.mat_tier)
    if req_tier:
        res = res[res["_tier"] >= req_tier]           # только апгрейд стали
        if not len(res):
            return pd.DataFrame(), (f"запрошена сталь {req_mat} — замена вниз "
                                    f"недопустима, аналогов не найдено")

    req_th = cust.get("thickness")
    if req_th is not None:
        th0 = pd.to_numeric(res["Толщина / Высота"], errors="coerce")
        res = res[(th0 - req_th).abs().fillna(999) <= 1.3]
        if not len(res):
            return pd.DataFrame(), (f"толщина {req_th} не имеет точного совпадения, "
                                    f"другие толщины невзаимозаменяемы")

    th = pd.to_numeric(res["Толщина / Высота"], errors="coerce")
    res["_th_d"] = (th - req_th).abs() if req_th is not None else 0.0
    res["_mat_d"] = (res["_tier"] - req_tier) if req_tier else 0
    res = res.sort_values(
        ["_mat_d", "_th_d", "Цена_без_НДС"],
        key=lambda s: pd.to_numeric(s, errors="coerce").fillna(9999))
    res = res.head(core.ANALOG_TOP).copy()

    def label(r):
        parts = []
        if joint_swap and str(r.get("Тип_изделия")) != cust.get("type"):
            parts.append(f"совместимый тип исполнения (стык {_joint_label(cust['joint'])})")
        if req_tier and r["_mat_d"] > 0:
            parts.append(f"апгрейд стали: {r['Материал']} вместо {req_mat}")
        if req_th is not None and pd.notna(r["Толщина / Высота"]) and r["_th_d"] > 0.01:
            parts.append(f"толщина {r['Толщина / Высота']} вместо {req_th}")
        return "; ".join(parts) if parts else "совпадение по размеру и давлению"

    res["Причина"] = res.apply(label, axis=1)
    note = f"аналогов: {len(res)}" + (f"; {extra_note}" if extra_note else "")
    return res, note


def _snp_config(std, typ):
    """core.TYPE_CONFIG[(Стандарт, Тип_изделия)] -> код конфигурации колец
    (NONE/INNER/OUTER/BOTH) или None, если сочетание не известно."""
    return core.TYPE_CONFIG.get((str(std), str(typ)))


# Итерация 25 (перформанс-хотфикс SELECTION_SPEC §11): find_cross_analogs и
# _diagnose_cross_analogs раньше парсили 'Основные_размеры' построчно через
# core.norm_dims на КАЖДЫЙ вызов (O(кандидатов) регэксп-разборов на строку
# выделения; тест 50 вызывает diagnose_analogs >=200 раз на одном df ->
# O(N_база x N_кандидатов) разборов одного и того же df). _dims_frame(df)
# разбирает df ОДИН раз в числовые колонки _d1.._d4 (NaN, если ключа нет) и
# кэширует результат по (id(df), len(df)) — df каталога в приложении
# неизменяем в рамках сессии (новый build создаёт новый df), len() в ключе —
# дешёвая защита от коллизии id() после сборки мусора старого df. Кэш держит
# только последнюю запись (не растёт неограниченно при работе с несколькими
# df подряд, напр. synth_cand в тестах).
_DIMS_CACHE: dict = {}


def _dims_frame(df: pd.DataFrame) -> pd.DataFrame:
    key = (id(df), len(df))
    cached = _DIMS_CACHE.get(key)
    if cached is not None:
        return cached
    parsed = df["Основные_размеры"].map(core.norm_dims)
    cols = {f"_{k}": parsed.map(lambda d, k=k: d.get(k, float("nan")))
            for k in ("d1", "d2", "d3", "d4")}
    out = pd.DataFrame(cols, index=df.index)
    _DIMS_CACHE.clear()
    _DIMS_CACHE[key] = out
    return out


def restrictor_kind(row) -> str:
    """Различимость двух ценовых групп СНП (итерация 27, взамен скрытия
    итерации 17): «(ограничитель как навитая часть)» в Маркировка_в_прайсе
    -> ограничитель той же намотки, что уплотнение («как навитая часть»);
    иначе, если у (Стандарт, Тип_изделия) есть наружное кольцо по core.
    TYPE_CONFIG (OUTER/BOTH) -> «из углеродки» — разведка итерации 27
    показала, что fullmetal-СУФФИКС (вторая ценовая группа) существует
    ТОЛЬКО у ГОСТ Г/Д (472/408 строк из 944/816); у ОСТ Г/Д и ASME В/Г/Д
    наружное кольцо тоже есть (OUTER/BOTH), но альтернативы «как навитая»
    в этих листах прайса нет вовсе — там единственная цена всегда «из
    углеродки»; иначе (NONE/INNER — нет наружного кольца, либо категория
    вне СНП, где TYPE_CONFIG не определён) -> «без ограничителя»."""
    mk = str(row.get("Маркировка_в_прайсе") or "")
    if "как навитая" in mk:
        return "как навитая часть"
    cfg = core.TYPE_CONFIG.get((str(row.get("Стандарт")), str(row.get("Тип_изделия"))))
    if cfg in ("OUTER", "BOTH"):
        return "из углеродки"
    return "без ограничителя"


def find_joint_analogs(df: pd.DataFrame, row) -> pd.DataFrame:
    """Аналоги «выделенной строки» по семье стыка исполнений (итерация 26,
    SELECTION_SPEC, раздел «Стыки исполнений»): ТОТ ЖЕ Стандарт, ДРУГОЙ
    Тип_изделия внутри core.JOINT_TYPE_FAMILY[joint] (joint = core.EXEC_TO_
    JOINT[Исполнение] базовой строки — по колонке, не по cust, работает и
    для строк, найденных НЕ через joint-заявку), тот же Материал, то же
    давление (Давление_кгс_мин/макс совпадают с базовой строкой — разведка
    §12 подтвердила, что у пар одного стыка одного DN это так же, как у
    d2/d3/d4), Основные_размеры в допуске core.CROSS_TOL (тот же guard
    полноты ключей d2/d3[+d4], что find_cross_analogs).

    Сознательно НЕ «кросс-стандарт» (find_cross_analogs) с ослабленным
    условием буквы — это отдельная функция ВНУТРИ одного стандарта: у СНП
    Г/Д (BB) кросс-стандартное сравнение по core.TYPE_CONFIG (итерация 23)
    уже корректно относит их к разным конфигурациям (OUTER/BOTH) и не
    считает их взаимозаменяемыми МЕЖДУ стандартами — расширять
    find_cross_analogs парами одного стыка означало бы ослабить тот самый
    guard, ради которого итерации 19/23 отдельно доказывали, что совпадение
    по 2 ключам недостаточно дискриминативно (риск фантомных кросс-«аналогов»
    без новой, отдельной разведки допуска между КАЖДОЙ парой стандартов —
    не сделана в этой итерации, поэтому find_cross_analogs не тронут)."""
    if str(row.get("Категория")) != "СНП":
        return pd.DataFrame()
    joint = core.EXEC_TO_JOINT.get(str(row.get("Исполнение")))
    if joint is None:
        return pd.DataFrame()
    family = core.JOINT_TYPE_FAMILY.get(joint, set())
    own_type = str(row.get("Тип_изделия"))
    others = family - {own_type}
    if not others:
        return pd.DataFrame()
    base = core.norm_dims(row.get("Основные_размеры"))
    if "d2" not in base or "d3" not in base:
        return pd.DataFrame()

    cand = df[(df["Категория"] == "СНП") & (df["Стандарт"] == row.get("Стандарт"))
              & (df["Тип_изделия"].isin(others))
              & (df["Материал"] == row.get("Материал"))]
    if not len(cand):
        return pd.DataFrame()

    p_lo = _num(row.get("Давление_кгс_мин"))
    p_hi = _num(row.get("Давление_кгс_макс"))
    if p_lo is not None and p_hi is not None:
        cand_lo = pd.to_numeric(cand["Давление_кгс_мин"], errors="coerce")
        cand_hi = pd.to_numeric(cand["Давление_кгс_макс"], errors="coerce")
        cand = cand[(cand_lo - p_lo).abs() < 1e-9]
        cand_hi = cand_hi.loc[cand.index]
        cand = cand[(cand_hi - p_hi).abs() < 1e-9]
    if not len(cand):
        return pd.DataFrame()

    dims = _dims_frame(df).loc[cand.index]
    mask = (dims["_d2"].notna() & (dims["_d2"] - float(base["d2"])).abs().le(core.CROSS_TOL)
            & dims["_d3"].notna() & (dims["_d3"] - float(base["d3"])).abs().le(core.CROSS_TOL))
    if "d4" in base:
        has_d4 = dims["_d4"].notna()
        d4_ok = (dims["_d4"] - float(base["d4"])).abs() <= core.CROSS_TOL
        mask &= (~has_d4) | d4_ok

    out = cand[mask].copy()
    if len(out):
        out["Причина"] = f"совместимый тип исполнения (стык {_joint_label(joint)})"
    return out


def find_cross_analogs(df: pd.DataFrame, row) -> pd.DataFrame:
    """Кросс-стандарт, допуск core.CROSS_TOL мм.

    У категорий вне СНП — прежний путь: core.cross_keys(cat) (напр. D/b/h
    для овальных), кандидат = та же буква Тип_изделия, другой стандарт.

    У СНП (итерация 23, разведка §11 SELECTION_SPEC): кандидат сравнивается
    по КОНФИГУРАЦИИ колец (core.TYPE_CONFIG), А НЕ ПО БУКВЕ типа — буква
    одинакова по написанию, но не по смыслу между стандартами (у ASME «В» —
    конфигурация OUTER, как «Г», а у ГОСТ/ОСТ «В» — INNER; «Б» существует
    только у ОСТ и по конфигурации совпадает с «А»). Ключи сравнения по
    конфигурации: NONE -> (d1,d2) — у NONE-конфигурации (буквы А и Б,
    везде) в этом прайсе НИКОГДА не бывает d1 ни у кого — это НАМЕРЕННЫЙ
    guard (не «буква А/Б запрещена», а «двух ключей d2/d3 недостаточно,
    чтобы отличить совпадение от разных типоразмеров» — реальный промах
    итерации 19, СНП-А-2-3-25 ГОСТ ~ ОСТ А-3-43); INNER/OUTER/BOTH -> (d2,d3)
    [+d4, если есть в ОБЕИХ строках] — тот же принцип guard'а полноты, что и
    в итерации 19, применяется по конфигурации, а не по букве."""
    cat = str(row.get("Категория"))
    if cat != "СНП":
        keys = core.cross_keys(cat)
        if not keys:
            return pd.DataFrame()
        base = core.norm_dims(row.get("Основные_размеры"))
        if any(k not in base for k in keys):
            return pd.DataFrame()
        cand = df[(df["Категория"] == row["Категория"])
                  & (df["Стандарт"] != row["Стандарт"])
                  & (df["Тип_изделия"] == row["Тип_изделия"])
                  & (df["Материал"] == row["Материал"])]
        reason_bits = "/".join(keys)
    else:
        config = _snp_config(row.get("Стандарт"), row.get("Тип_изделия"))
        if config is None:
            return pd.DataFrame()
        keys = ["d1", "d2"] if config == "NONE" else ["d2", "d3"]
        base = core.norm_dims(row.get("Основные_размеры"))
        if any(k not in base for k in keys):
            return pd.DataFrame()
        cand_cfg = pd.Series([_snp_config(s, t) for s, t in
                              zip(df["Стандарт"], df["Тип_изделия"])], index=df.index)
        cand = df[(df["Категория"] == "СНП")
                  & (df["Стандарт"] != row["Стандарт"])
                  & (cand_cfg == config)
                  & (df["Материал"] == row["Материал"])]
        reason_bits = f"конфигурация {config}, " + "/".join(keys)
    if not len(cand):
        return pd.DataFrame()

    # итерация 25: маски по колонкам _dims_frame вместо построчного
    # core.norm_dims + Python-цикла ok() — семантика eff_keys не менялась
    # (d4 фильтрует только кандидатов, у которых d4 присутствует; base без
    # d4 не требует его от кандидата вовсе).
    dims = _dims_frame(df).loc[cand.index]
    mask = pd.Series(True, index=cand.index)
    for k in keys:
        mask &= dims[f"_{k}"].notna() & ((dims[f"_{k}"] - float(base[k])).abs() <= core.CROSS_TOL)
    if "d4" in base:
        has_d4 = dims["_d4"].notna()
        d4_ok = (dims["_d4"] - float(base["d4"])).abs() <= core.CROSS_TOL
        mask &= (~has_d4) | d4_ok

    out = cand[mask].copy()
    if len(out):
        out["Причина"] = f"кросс-стандарт ({reason_bits}, допуск {core.CROSS_TOL} мм)"
    return out


def find_cross_nominal_analogs(df: pd.DataFrame, row) -> pd.DataFrame:
    """Tier 2 («номинал», итерация 28, SELECTION_SPEC «Двухуровневые кросс-
    аналоги»): та же категория/конфигурация и материал, ДРУГОЙ стандарт, тот
    же DN_num (номинальная маркировка размера) — но размеры (d2/d3[/d4] у
    СНП, ключи core.cross_keys(cat) у прочих категорий) НЕ все в допуске
    core.CROSS_TOL. Строки, которые уже прошли бы допуск, здесь исключаются
    (~fit-маска) — они и так возвращаются find_cross_analogs (tier 1,
    «посадка»), дублировать их tier 2 не должен.

    Разведка итерации 28 (СНП, «Овальные», «Восьмиугольные»): ГОСТ и ASME
    кодируют DN разными системами нумерации одного физического размера
    (итерации 19/23) — поэтому у СНП Г/Д (config OUTER/BOTH) совпадение
    DN_num между ГОСТ и ASME почти всегда означает РАЗНЫЙ физический размер
    («номинал» совпал по цифре, не по факту посадки: 32 пары, Δd2/Δd3 от
    0.4/1.4 до 22.3/2.1 мм); у типа А (config NONE) — 0 пар, тот же guard
    отсутствия d1 в этом прайсе, что и у find_cross_analogs/
    _diagnose_cross_analogs (§8/A13); ОСТ не делит DN_num ни с ГОСТ, ни с
    ASME для Г/Д в этом каталоге — реальные tier2-пары в данных только
    ASME↔ГОСТ. «Овальные»/«Восьмиугольные»: 0 пар — cross_keys(cat) для
    «Овальные» требует ключ "D", которого нет ни у одной строки категории
    (реальные ключи «Основные_размеры» — OD/ID/P/A/В), «Восьмиугольные» не
    имеет cross_keys вовсе (core.cross_keys возвращает [] для всех категорий,
    кроме «СНП»/«Овальные») — тот же guard, что уже отключает tier 1 для
    этих категорий (core.py не менялся, ключи не расширялись).

    Причина каждой строки — «номинал: тот же DN в <Стандарт>; Δd2=…, Δd3=…
    (Δd4=…) — проверить посадку»; результат отсортирован по сумме дельт (по
    возрастанию) и урезан до 6 строк."""
    cat = str(row.get("Категория"))
    dn = row.get("DN_num")
    if dn is None or pd.isna(dn):
        return pd.DataFrame()
    dn = float(dn)
    if cat != "СНП":
        keys = core.cross_keys(cat)
        if not keys:
            return pd.DataFrame()
        base = core.norm_dims(row.get("Основные_размеры"))
        if any(k not in base for k in keys):
            return pd.DataFrame()
        cand = df[(df["Категория"] == cat)
                  & (df["Стандарт"] != row["Стандарт"])
                  & (df["Тип_изделия"] == row["Тип_изделия"])
                  & (df["Материал"] == row["Материал"])
                  & (pd.to_numeric(df["DN_num"], errors="coerce") == dn)]
    else:
        config = _snp_config(row.get("Стандарт"), row.get("Тип_изделия"))
        if config is None:
            return pd.DataFrame()
        keys = ["d1", "d2"] if config == "NONE" else ["d2", "d3"]
        base = core.norm_dims(row.get("Основные_размеры"))
        if any(k not in base for k in keys):
            return pd.DataFrame()
        cand_cfg = pd.Series([_snp_config(s, t) for s, t in
                              zip(df["Стандарт"], df["Тип_изделия"])], index=df.index)
        cand = df[(df["Категория"] == "СНП")
                  & (df["Стандарт"] != row["Стандарт"])
                  & (cand_cfg == config)
                  & (df["Материал"] == row["Материал"])
                  & (pd.to_numeric(df["DN_num"], errors="coerce") == dn)]
    if not len(cand):
        return pd.DataFrame()

    dims = _dims_frame(df).loc[cand.index]
    fit = pd.Series(True, index=cand.index)
    for k in keys:
        fit &= dims[f"_{k}"].notna() & ((dims[f"_{k}"] - float(base[k])).abs() <= core.CROSS_TOL)
    if "d4" in base:
        has_d4 = dims["_d4"].notna()
        d4_ok = (dims["_d4"] - float(base["d4"])).abs() <= core.CROSS_TOL
        fit &= (~has_d4) | d4_ok

    out = cand[~fit].copy()
    if not len(out):
        return out
    out_dims = dims.loc[out.index]
    deltas = {k: (out_dims[f"_{k}"] - float(base[k])).abs() for k in keys}
    score = sum(deltas.values())
    d4d = (out_dims["_d4"] - float(base["d4"])).abs() if "d4" in base else None

    def _reason(idx):
        std = out.loc[idx, "Стандарт"]
        bits = ", ".join(f"Δ{k}={deltas[k].loc[idx]:.1f}" for k in keys)
        if d4d is not None and pd.notna(d4d.loc[idx]):
            bits += f" (Δd4={d4d.loc[idx]:.1f})"
        return f"номинал: тот же DN в {std}; {bits} — проверить посадку"

    out["Причина"] = [_reason(idx) for idx in out.index]
    out = out.assign(_score=score.values)
    out = out.sort_values("_score", kind="mergesort").head(6).drop(columns=["_score"])
    return out


def _brand_key(s: str) -> str:
    s = str(s).upper().replace("Ё", "Е")
    s = re.sub(r"НАБИВКА|ЛЕНТА", " ", s)
    m = re.match(r"^([A-ZА-Я0-9\s\-]+?)(?:\s+\d+[ХX*]\d+|$)", s)
    if m:
        s = m.group(1)
    s = "".join(core.CYR2LAT.get(ch, ch) for ch in s)
    return re.sub(r"[^A-Z0-9]", "", s)


def _brand_group_of(mark: str):
    nm = _brand_key(mark)
    if not nm:
        return None
    groups = core.load_brand_groups()
    for gi, grp in enumerate(groups):
        for _brand, tok in grp:
            tn = _brand_key(tok)
            if nm == tn or nm.startswith(tn) or tn.startswith(nm):
                return gi
    return None


def find_brand_analogs(df: pd.DataFrame, row) -> pd.DataFrame:
    """Кросс-брендовые аналоги набивки по BRAND_GROUPS (core.load_brand_groups)."""
    gi = _brand_group_of(str(row.get("Маркировка_в_прайсе")))
    if gi is None:
        return pd.DataFrame()
    groups = core.load_brand_groups()
    pack = df[df["Категория"] == "Набивки"]
    # ключи брендов набивочного набора считаем один раз за вызов, а не на
    # каждый бренд группы — раньше iterrows() по pack гонялся внутри цикла
    # по брендам (O(n_brands * n_pack)), теперь O(n_pack + n_brands).
    pack_keys = [(_brand_key(str(m)), r) for m, r in
                 zip(pack["Маркировка_в_прайсе"], pack.to_dict("records"))]
    self_key = _brand_key(str(row.get("Маркировка_в_прайсе")))
    rows = []
    for brand, tok in groups[gi]:
        tn = _brand_key(tok)
        if tn == self_key:
            continue
        hit = None
        for cm, r in pack_keys:
            if cm.startswith(tn) or tn.startswith(cm):
                hit = r
                break
        if hit is not None:
            d = dict(hit)
            d["Причина"] = f"бренд: {brand}"
            rows.append(d)
    return pd.DataFrame(rows)


# ============================ ДИАГНОСТИКА ОТСУТСТВИЯ АНАЛОГОВ ============================
# Итерация 23: чисто информационная функция (find_cross_analogs/
# find_brand_analogs не меняет) — статус-строка GUI, когда обе таблицы
# аналогов для выделенной строки пусты.

def _dn_label(dn) -> str:
    """DN_num -> «DN450» (целое), а не «DN450.0» — DN_num в каталоге float."""
    if pd.isna(dn):
        return "DN?"
    return f"DN{int(round(float(dn)))}"


def _diagnose_cross_analogs(df, row) -> str:
    """Зеркалирует guard'ы find_cross_analogs, В ТОМ ЖЕ ПОРЯДКЕ проверки на
    каждого кандидата (итерация 24, хотфикс SELECTION_SPEC §11): (а) полнота
    ключей размерности базы — у СНП с конфигурацией NONE (буквы А/Б везде) в
    этом прайсе никогда нет d1, тот же guard, что итерация 19 ввела для буквы
    «А» (§8/A13); (б) конфигурация колец; (в) допуск (core.CROSS_TOL);
    (г) материал. Guard (а) — ГЛОБАЛЬНЫЙ (find_cross_analogs проверяет его до
    перебора кандидатов и, если он не пройден, отбрасывает буквально ВСЕХ
    кандидатов разом), поэтому в диагностике он подставляется КАЖДОМУ
    кандидату, у которого больше не осталось иной причины — если у кандидата
    уже есть содержательная причина (б/в/г), она информативнее и остаётся
    единственной; если содержательной причины нет (кандидат совпадает по
    конфигурации/допуску/материалу — как ОСТ А/Б DN43 против типа А DN25), то
    единственная настоящая причина отказа — guard (а), а не «должен был
    найтись». Топ-3 ближайших кандидата другого стандарта по |Δd2|+|Δd3|,
    сгруппированных по (Стандарт, Тип, DN_num) — одна строка на группу, число
    строк-толщин в скобках. Пустая строка, если категория не поддерживает
    кросс-стандарт вовсе (core.cross_keys(cat) пуст и это не СНП)."""
    cat = str(row.get("Категория"))
    if cat != "СНП" and not core.cross_keys(cat):
        return ""
    base = core.norm_dims(row.get("Основные_размеры"))
    if "d2" not in base or "d3" not in base:
        return "кросс-стандарт: у этой строки нет размеров d2/d3 для сравнения"

    config = _snp_config(row.get("Стандарт"), row.get("Тип_изделия")) if cat == "СНП" else None
    guard_reason = None
    if cat == "СНП" and config is not None:
        guard_keys = ["d1", "d2"] if config == "NONE" else ["d2", "d3"]
        missing = [k for k in guard_keys if k not in base]
        if missing:
            if config == "NONE":
                guard_reason = (f"недостаточная размерность: у типа "
                                 f"{row.get('Тип_изделия')} нет d1, кросс-стандарт "
                                 f"отключён (§8/A13)")
            else:
                guard_reason = f"недостаточная размерность: нет {'/'.join(missing)}"

    cand = df[(df["Категория"] == cat) & (df["Стандарт"] != row.get("Стандарт"))]
    if not len(cand):
        return f"кросс-стандарт: в каталоге нет строк категории «{cat}» другого стандарта"

    # итерация 25: разбор размеров через _dims_frame (кэш, не построчный
    # core.norm_dims) + векторные дельты по колонкам; группировка по
    # (Стандарт, Тип, DN_num) остаётся через groupby (толщина/материал
    # размножают одну физическую позицию на много строк каталога, топ-3
    # должны быть 3 РАЗНЫМИ типоразмерами) — внутри группы берём строку с
    # минимальным score (|Δd2|+|Δd3|) как представителя + число уникальных
    # толщин.
    dims = _dims_frame(df).loc[cand.index]
    valid = dims["_d2"].notna() & dims["_d3"].notna()
    cand = cand[valid]
    dims = dims[valid]
    if not len(cand):
        return "кросс-стандарт: у кандидатов другого стандарта нет размеров d2/d3"

    d2delta_s = (dims["_d2"] - base["d2"]).abs()
    d3delta_s = (dims["_d3"] - base["d3"]).abs()
    score_s = d2delta_s + d3delta_s
    if "d4" in base:
        d4delta_s = (dims["_d4"] - base["d4"]).abs()
    else:
        d4delta_s = pd.Series(float("nan"), index=cand.index)

    grp = pd.DataFrame({
        "std": cand["Стандарт"].values,
        "typ": cand["Тип_изделия"].values,
        "dn": cand["DN_num"].values,
        "score": score_s.values,
        "d2delta": d2delta_s.values,
        "d3delta": d3delta_s.values,
        "d4delta": d4delta_s.values,
        "thick": cand["Толщина / Высота"].values,
    }, index=cand.index)
    key_cols = ["std", "typ", "dn"]
    # dn (DN_num) может быть NaN у отдельных строк -- MultiIndex.loc по
    # ключу с NaN бросает KeyError (в отличие от обычного dict), поэтому
    # исходный индекс представителя группы (orig_idx) везём отдельной
    # колонкой вместо повторного лукапа по (std, typ, dn).
    rep_idx = grp.groupby(key_cols, dropna=False)["score"].idxmin()
    counts = grp.groupby(key_cols, dropna=False)["thick"].nunique().reindex(rep_idx.index)
    reps = grp.loc[rep_idx.values].copy()
    reps["n"] = counts.values
    reps["orig_idx"] = rep_idx.values
    scored = reps.sort_values("score").head(3)

    parts = []
    for _, g in scored.iterrows():
        std, typ, dn = g["std"], g["typ"], g["dn"]
        n = int(g["n"])
        r = cand.loc[g["orig_idx"]]
        d2delta, d3delta = g["d2delta"], g["d3delta"]
        # d4 (наружное кольцо) — тот же ключ, что find_cross_analogs учитывает
        # в eff_keys, когда он есть в ОБЕИХ строках (use_d4); без этой
        # проверки диагностика молчала про реальную причину отказа у пар
        # BOTH/OUTER, совпавших по d2/d3, но разошедшихся по d4 (найдено
        # тестом 50 на ОСТ-Д-DN179 ~ ГОСТ-Д-DN150).
        d4delta = None if pd.isna(g["d4delta"]) else float(g["d4delta"])
        reasons = []
        if cat == "СНП":
            cand_cfg = _snp_config(std, typ)
            if cand_cfg != config:
                reasons.append(f"конфигурация {cand_cfg} ≠ {config}")
        tol_exceeded = d2delta > core.CROSS_TOL or d3delta > core.CROSS_TOL
        if d4delta is not None and d4delta > core.CROSS_TOL:
            tol_exceeded = True
        if tol_exceeded:
            reasons.append(f"допуск >{core.CROSS_TOL} мм")
        if str(r.get("Материал")) != str(row.get("Материал")):
            reasons.append("материал")
        if not reasons and guard_reason:
            reasons.append(guard_reason)
        reason_txt = (", ".join(reasons) if reasons else
                      "причина не определена: рассинхрон диагностики и фильтров")
        n_txt = f", ×{n} толщины" if n > 1 else ""
        d4_txt = f", Δd4={d4delta:.1f}" if d4delta is not None else ""
        parts.append(f"{std} {typ} {_dn_label(dn)}: Δd2={d2delta:.1f}, "
                     f"Δd3={d3delta:.1f}{d4_txt}{n_txt} ({reason_txt})")
    return "кросс-стандарт, ближайшие кандидаты: " + "; ".join(parts)


def _diagnose_brand_analogs(row) -> str:
    """Пояснение для find_brand_analogs (только категория «Набивки»)."""
    if str(row.get("Категория")) != "Набивки":
        return "брендовые аналоги только для категории «Набивки»"
    if _brand_group_of(str(row.get("Маркировка_в_прайсе"))) is None:
        return "маркировка не входит ни в одну группу брендов (core.load_brand_groups)"
    return ""


def diagnose_analogs(df: pd.DataFrame, row) -> str:
    """Почему find_cross_analogs/find_brand_analogs вернули 0 строк для этой
    выделенной позиции (итерация 23) — статус-строка GUI вместо молчаливого
    «Аналогов: 0». Диагностика по КАЖДОЙ подсистеме считается только если
    сама подсистема реально дала 0 (иначе строка была бы непустой даже при
    наличии настоящих аналогов — она не про «а что ещё рядом», а строго про
    «почему аналогов нет»). Первая строка — кросс-диагностика (если
    применима), дальше — брендовая. Пустая строка, если аналоги ЕСТЬ у
    обеих подсистем (или подсистема неприменима к этой категории)."""
    lines = []
    if not len(find_cross_analogs(df, row)):
        msg = _diagnose_cross_analogs(df, row)
        if msg:
            lines.append(msg)
    if not len(find_brand_analogs(df, row)):
        msg = _diagnose_brand_analogs(row)
        if msg:
            lines.append(msg)
    return "\n".join(lines)


# ============================ DIFF ЦЕН МЕЖДУ ПЕРЕСБОРКАМИ ============================
# Итерация 11: entry.py prices-diff сравнивает текущий каталог (catalog) с
# предыдущим срезом (catalog_prev, db.upload_to_db сохраняет его перед DROP).

def price_diff(df_new: pd.DataFrame, df_old: pd.DataFrame, top_n: int = 15):
    """Сравнение цен между двумя срезами каталога (join по SKU).

    NULL-цена (в новом ИЛИ старом срезе) исключает пару из сравнения цены —
    такой SKU не считается ни «изменился», ни «добавлен»/«удалён» (он есть
    в обоих срезах, просто цена неизвестна хотя бы на одной стороне).

    Возвращает (summary, top): summary — {added, removed, changed_up,
    changed_down}; top — DataFrame топ-N по |Δ%| (SKU/Маркировка/цена
    была/стала/Δ/Δ%/«было→стало»), отсортированный по убыванию |Δ%|."""
    new = df_new[["SKU", "Маркировка_в_прайсе", "Цена_без_НДС"]].copy()
    old = df_old[["SKU", "Маркировка_в_прайсе", "Цена_без_НДС"]].copy()
    new["Цена_без_НДС"] = pd.to_numeric(new["Цена_без_НДС"], errors="coerce")
    old["Цена_без_НДС"] = pd.to_numeric(old["Цена_без_НДС"], errors="coerce")

    new_skus, old_skus = set(new["SKU"]), set(old["SKU"])
    added, removed = len(new_skus - old_skus), len(old_skus - new_skus)

    merged = new.merge(old, on="SKU", how="inner", suffixes=("_new", "_old"))
    priced = merged["Цена_без_НДС_new"].notna() & merged["Цена_без_НДС_old"].notna()
    cmp = merged[priced].copy()
    cmp["delta"] = cmp["Цена_без_НДС_new"] - cmp["Цена_без_НДС_old"]
    cmp = cmp[cmp["delta"] != 0]
    changed_up = int((cmp["delta"] > 0).sum())
    changed_down = int((cmp["delta"] < 0).sum())

    cmp["delta_pct"] = cmp["delta"] / cmp["Цена_без_НДС_old"] * 100.0
    top = cmp.reindex(cmp["delta_pct"].abs().sort_values(ascending=False).index)
    top = top.head(top_n).copy()
    top["было→стало"] = top.apply(
        lambda r: f"{r['Цена_без_НДС_old']:.2f} → {r['Цена_без_НДС_new']:.2f}", axis=1)
    top = top.rename(columns={"Маркировка_в_прайсе_new": "Маркировка",
                              "Цена_без_НДС_old": "Цена_была",
                              "Цена_без_НДС_new": "Цена_стала"})
    top = top[["SKU", "Маркировка", "Цена_была", "Цена_стала",
              "delta", "delta_pct", "было→стало"]].reset_index(drop=True)

    summary = {"added": added, "removed": removed,
              "changed_up": changed_up, "changed_down": changed_down}
    return summary, top


# ============================ ИМПОРТ ЗАЯВКИ ИЗ ФАЙЛА ============================
# Чистая функция, без tkinter — GUI (gui.py) только вызывает parse_request_file
# и вставляет результат в поле заявок.

_MARK_SYNONYMS = {"маркировка", "обозначение", "наименование", "позиция", "mark"}
_QTY_SYNONYMS = {"кол-во", "количество", "qty", "шт"}
# итерация 14: опциональные колонки давления/толщины/материала (по синонимам
# заголовка) — если найдены, синтезированная строка заявки сужает подбор до
# конкретного варианта, а не разворачивает всю линейку толщин/давлений.
_PRESSURE_SYNONYMS = {"давление", "pn", "ру"}
_THICKNESS_SYNONYMS = {"толщина", "высота", "h"}
_MATERIAL_SYNONYMS = {"материал", "сталь", "марка"}
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _read_table_rows(path) -> list:
    """xlsx/xlsm — первый лист через pandas; csv — разделитель ';'/',' автопробой
    по первым строкам. Возвращает список строк (список списков ячеек)."""
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        df = pd.read_excel(p, sheet_name=0, header=None, dtype=object)
        return [[None if pd.isna(v) else v for v in row] for row in df.values.tolist()]
    text = p.read_text(encoding="utf-8-sig", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    sample = "\n".join(lines[:5])
    delim = ";" if sample.count(";") >= sample.count(",") else ","
    return [list(row) for row in csv.reader(lines, delimiter=delim)]


def _qty_from_cell(v):
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    n = _num(s)
    return int(round(n)) if n is not None else None


def _first_numeric_col(rows, exclude_col=0):
    ncols = max((len(r) for r in rows), default=0)
    for c in range(ncols):
        if c == exclude_col:
            continue
        if any(c < len(r) and _num(r[c]) is not None for r in rows):
            return c
    return None


def _pressure_token(cell) -> str:
    """Давление-ячейка -> функциональный токен "PN{n1}МПа[-{n2}МПа]", который
    parse_customer_line штатно распознаёт как явное "PN N МПа" (ту же ветку,
    что и ручной ввод "PN 1,6 МПа") и переводит в кгс (×10). Список вида
    "1.6-2.5-4.0" сводится к ПЕРВЫМ ДВУМ числам — видимый диапазон в строке
    заявки; единица "МПа" повторяется у каждого числа, а не одна на весь
    диапазон — regex явного PN ([\\d.,]+ без дефиса) захватывает только
    первое число и потерял бы суффикс единицы у второго, если написать её
    один раз в конце. Пустая/нечисловая ячейка -> "" (пропускается)."""
    if cell is None or str(cell).strip() == "":
        return ""
    nums = _NUM_RE.findall(str(cell))
    if not nums:
        return ""
    c = lambda n: n.replace(".", ",")
    if len(nums) >= 2:
        return f"PN{c(nums[0])}МПа-{c(nums[1])}МПа"
    return f"PN{c(nums[0])}МПа"


def _thickness_token(cell) -> str:
    """Толщина-ячейка -> "толщ N" (parse_customer_line's толщина-regex ищет
    это слово где угодно в строке, не привязано к позиции маркировки)."""
    n = _num(cell) if cell is not None else None
    return f"толщ {n:g}" if n is not None else ""


def parse_request_file(path) -> list:
    """Читает файл заявки (xlsx/csv) и возвращает строки вида
    '{маркировка} [PN{давление}МПа] [толщ {толщина}] [{материал}] [{кол-во} шт]'
    (кол-во опускается, если 1/пусто; давление/толщина/материал — опциональны,
    появляются, только если такая колонка найдена и ячейка не пуста).

    Ищет строку заголовка в первых 10 строках по синонимам колонок
    (_MARK_SYNONYMS/_QTY_SYNONYMS/_PRESSURE_SYNONYMS/_THICKNESS_SYNONYMS/
    _MATERIAL_SYNONYMS); если заголовка нет — колонка 0 — маркировка, первая
    числовая колонка — кол-во (иначе кол-во 1), давление/толщина/материал не
    ищутся (только по заголовку — обратная совместимость с файлами без
    заголовка: они и раньше давали только «маркировка [+ кол-во]»). Пустые
    строки и строки-заголовки отбрасывает."""
    rows = _read_table_rows(path)
    if not rows:
        return []

    header_row_idx = None
    mark_col, qty_col = 0, None
    pressure_col = thickness_col = material_col = None
    for i, row in enumerate(rows[:10]):
        mc = qc = pc = tc = mtc = None
        for j, cell in enumerate(row):
            norm = str(cell).strip().lower() if cell is not None else ""
            if norm in _MARK_SYNONYMS and mc is None:
                mc = j
            if norm in _QTY_SYNONYMS and qc is None:
                qc = j
            if norm in _PRESSURE_SYNONYMS and pc is None:
                pc = j
            if norm in _THICKNESS_SYNONYMS and tc is None:
                tc = j
            if norm in _MATERIAL_SYNONYMS and mtc is None:
                mtc = j
        if mc is not None:
            header_row_idx, mark_col, qty_col = i, mc, qc
            pressure_col, thickness_col, material_col = pc, tc, mtc
            break

    if header_row_idx is not None:
        data_rows = rows[header_row_idx + 1:]
    else:
        data_rows = rows
        qty_col = _first_numeric_col(rows, exclude_col=mark_col)

    out = []
    for row in data_rows:
        mark = (str(row[mark_col]).strip()
                if mark_col < len(row) and row[mark_col] is not None else "")
        if not mark or mark.lower() in _MARK_SYNONYMS:
            continue
        qty = _qty_from_cell(row[qty_col]) if qty_col is not None and qty_col < len(row) else None

        parts = [mark]
        if pressure_col is not None and pressure_col < len(row):
            tok = _pressure_token(row[pressure_col])
            if tok:
                parts.append(tok)
        if thickness_col is not None and thickness_col < len(row):
            tok = _thickness_token(row[thickness_col])
            if tok:
                parts.append(tok)
        if (material_col is not None and material_col < len(row)
                and row[material_col] not in (None, "")):
            parts.append(str(row[material_col]).strip())
        line = " ".join(parts)
        if qty not in (None, 1):
            line += f" {qty} шт"
        out.append(line)
    return out


# ============================ КП: СЛИЯНИЕ ДУБЛЕЙ ============================

def merge_or_append_kp(kp_list, item, qty):
    """Чистый хелпер (итерация 21, без tkinter): защита КП от дублей SKU при
    повторном добавлении. Не мутирует kp_list — возвращает НОВЫЙ список,
    чтобы вызывающий (GUI) мог решить, сохранять ли результат, ПОСЛЕ диалога
    подтверждения (askyesno между обнаружением дубля и записью состояния).

    Если `item["sku"]` уже есть в kp_list — действие "merge": количество
    СУЩЕСТВУЮЩЕЙ строки увеличивается на `qty`, остальные поля (включая
    цену) берутся от существующей строки, не от `item` — сумма (qty*price)
    у вызывающего пересчитывается от них же при отрисовке, а не хранится
    отдельно. Иначе — действие "append": `item` с `qty` добавляется в конец
    списка, существующие строки не трогаются.

    Возвращает (new_kp_list, action), action ∈ {"merge", "append"}."""
    for i, existing in enumerate(kp_list):
        if existing.get("sku") == item.get("sku"):
            merged = dict(existing)
            merged["qty"] = existing.get("qty", 0) + qty
            new_list = list(kp_list)
            new_list[i] = merged
            return new_list, "merge"
    new_item = dict(item)
    new_item["qty"] = qty
    return list(kp_list) + [new_item], "append"


# ============================ СООТВЕТСТВИЯ ПОСТАВЩИКОВ (итерация 29) ============================

def group_row_mask(df, rules) -> pd.Series:
    """Итерация 33 (single-source): единственная реализация конъюнкции
    правил группы поставщиков (categories И standards И material_tokens И
    material_not И mark_tokens И mark_not) — векторная маска по всему df.
    До этой итерации одна и та же логика жила в ТРЁХ независимых копиях
    (entry.py::_supplier_group_mask, gui.py::_supplier_group_mask,
    supplier_audit.py::_group_mask) — добавление нового поля (mark_not,
    здесь же) требовало бы править все три синхронно и рисковало рассинхро-
    ном. Теперь suppliers_for (построчно, через 1-строчный df) и все три
    модуля выше вызывают ТОЛЬКО эту функцию. mark_not — симметрично
    material_not: наличие любого токена из mark_not в Маркировка_в_прайсе
    ИСКЛЮЧАЕТ строку (пример: «Кольца КГН по умолчанию Vendor C» —
    mark_not=[ПУТГ,ТРГ,ТМГ], эти три уходят отдельным правилом к Vendor B).
    rules["categories"] пусты/не заданы -> категория не фильтруется;
    rules["standards"] сравнивается точным значением (не подстрокой, в
    отличие от material/mark); material_tokens/mark_tokens — «содержит хотя
    бы один» (OR), material_not/mark_not — «не содержит НИ ОДНОГО» (AND NOT).
    Сравнение везде по .upper(), без учёта регистра."""
    rules = rules or {}
    cats = rules.get("categories") or []
    mask = df["Категория"].isin(cats) if cats else pd.Series(True, index=df.index)
    standards = rules.get("standards") or []
    if standards:
        mask &= df["Стандарт"].fillna("").astype(str).str.upper().isin(
            {s.upper() for s in standards})
    mat_tok = rules.get("material_tokens") or []
    if mat_tok:
        m = df["Материал"].fillna("").astype(str).str.upper()
        tm = pd.Series(False, index=df.index)
        for t in mat_tok:
            tm |= m.str.contains(t.upper(), regex=False)
        mask &= tm
    material_not = rules.get("material_not") or []
    if material_not:
        m = df["Материал"].fillna("").astype(str).str.upper()
        tm = pd.Series(False, index=df.index)
        for t in material_not:
            tm |= m.str.contains(t.upper(), regex=False)
        mask &= ~tm
    mark_tok = rules.get("mark_tokens") or []
    if mark_tok:
        mk = df["Маркировка_в_прайсе"].fillna("").astype(str).str.upper()
        tm = pd.Series(False, index=df.index)
        for t in mark_tok:
            tm |= mk.str.contains(t.upper(), regex=False)
        mask &= tm
    mark_not = rules.get("mark_not") or []
    if mark_not:
        mk = df["Маркировка_в_прайсе"].fillna("").astype(str).str.upper()
        tm = pd.Series(False, index=df.index)
        for t in mark_not:
            tm |= mk.str.contains(t.upper(), regex=False)
        mask &= ~tm
    return mask


def suppliers_for(row, groups) -> list:
    """Возможные поставщики для позиции каталога `row` по справочнику групп
    `groups` (core.load_supplier_groups()) — объединение suppliers всех
    групп, чьи rules совпали (конъюнкция — см. group_row_mask, единственная
    реализация с итерации 33: row оборачивается в 1-строчный df и проверяется
    той же векторной маской, что и entry.py/gui.py/supplier_audit.py).
    Порядок результата стабилен (порядок групп в справочнике, порядок
    suppliers внутри группы), дубли поставщика между группами убираются
    (первое вхождение остаётся)."""
    row_df = pd.DataFrame([row])
    out = []
    seen = set()
    for g in groups:
        mask = group_row_mask(row_df, g.get("rules") or {})
        if bool(mask.iloc[0]):
            for sup in g.get("suppliers") or []:
                if sup not in seen:
                    seen.add(sup)
                    out.append(sup)
    return out

def suppliers_for_frame(df, groups) -> list:
    """Итерация 36: векторный аналог suppliers_for для целого df — список
    списков поставщиков (по строке df, порядок групп/поставщиков и дедуп те
    же), плюс id первой покрывающей группы: [(suppliers, first_gid|None)]."""
    out = [([], None) for _ in range(len(df))]
    seen = [set() for _ in range(len(df))]
    for g in groups:
        mask = group_row_mask(df, g.get("rules") or {}).to_numpy()
        for i in mask.nonzero()[0]:
            sups, first = out[i]
            if first is None:
                out[i] = (sups, g.get("id"))
            for sup in g.get("suppliers") or []:
                if sup not in seen[i]:
                    seen[i].add(sup)
                    sups.append(sup)
    return out


def suppliers_for_row(row_dict, groups=None) -> list:
    """Итерация 36: все поставщики, покрывающие строку каталога по ВСЕМ
    группам supplier_groups.json (через group_row_mask). groups=None ->
    core.load_supplier_groups()."""
    if groups is None:
        groups = core.load_supplier_groups()
    return suppliers_for_frame(pd.DataFrame([dict(row_dict)]), groups)[0][0]
