#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build_mock.py — сквозной прогон на синтетических данных (один скрипт).

    python build_mock.py                # сборка + 5 демо-заявок
    python build_mock.py --query "..."  # своя строка заявки поверх сборки

Что делает:
  1. грузит синтетический каталог data/mock_catalog_seed.json (200 SKU);
  2. разбирает «грязный» прайс data/mock_prices.xlsx (заголовки по синонимам,
     омоглифы, диапазоны давлений, единицы, дубли, пропуски) и вливает его
     в каталог: обновление цен существующих SKU + новые позиции + карантин;
  3. пишет каталог в SQLite (out/sealmatch.sqlite: DROP+CREATE, 3 индекса,
     служебные таблицы price_updates / quarantine) и печатает SQL-сводку;
  4. читает каталог обратно из SQLite и прогоняет 5 заявок клиентов через
     ядро подбора src/matching.py (тот же код, что в рабочей версии):
     маршрутизация -> точный подбор -> аналоги (кросс-стандарт «посадка» /
     «номинал», бренд-эквиваленты) -> возможные поставщики -> диагностика
     промаха и ближайшие варианты;
  5. печатает отчёт: найдено / аналоги / промахи.

Зависимости: pandas, openpyxl (sqlite3 — стандартная библиотека).
Все данные синтетические, поставщики — абстрактные Vendor A..E.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = ROOT / "out"
sys.path.insert(0, str(ROOT / "src"))

import core      # noqa: E402  (src/ — обезличенная копия рабочего ядра)
import matching  # noqa: E402
import db_sqlite  # noqa: E402  (чтение каталога — общее с GUI)

SEED_FILE = DATA / "mock_catalog_seed.json"
PRICES_FILE = DATA / "mock_prices.xlsx"
SUPPLIERS_FILE = DATA / "mock_suppliers.json"
DB_FILE = OUT / "sealmatch.sqlite"

DEMO_REQUESTS = [
    # 1. маркировка ГОСТ: точный подбор + кросс-аналоги ОСТ («посадка») и ASME («номинал»)
    "СНП-Д-1-1-50-40 ГОСТ SS316L",
    # 2. набивка с опечаточным написанием кода и количеством: бренд-эквиваленты других поставщиков
    "Набивка AX-101 8х8 — 12 кг",
    # 3. без маркировки, только критерии DN/PN/сталь: фильтры по колонкам каталога
    "DN80 PN16 SS321",
    # 4. ASME: буква типа ≠ конфигурация колец (ASME «В» = OUTER, как ГОСТ «Г»)
    'СНП-В 4" RF CL300 SS304 ASME',
    # 5. несуществующий код: честный промах + диагностика + ближайшие варианты
    "Набивка AX 107 10x10",
]

# ============================ КАНОН СТРОКИ КАТАЛОГА ============================

STD_CODE = {"ГОСТ": "GOST", "ОСТ": "OST", "ASME": "ASME"}
TYPE_LAT = {"А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D"}
CAT_PREFIX = {"СНП": "SNP", "Овальные": "OVAL", "Восьмиугольные": "OCT", "Набивки": "PACK",
              "Прокладки": "GSK", "Кольца КГН": "KGN", "Ленты": "TAPE"}
GOST_EXEC = {"А": "2-3", "В": "2-3", "Г": "1-1", "Д": "1-1"}
_TRANSLIT = str.maketrans({
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E", "Ё": "E", "Ж": "ZH", "З": "Z",
    "И": "I", "Й": "I", "К": "K", "Л": "L", "М": "M", "Н": "N", "О": "O", "П": "P", "Р": "R",
    "С": "S", "Т": "T", "У": "U", "Ф": "F", "Х": "X", "Ц": "C", "Ч": "CH", "Ш": "SH", "Щ": "SH",
    "Ы": "Y", "Э": "E", "Ю": "YU", "Я": "YA", "Ь": "", "Ъ": ""})


def _slug(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s).upper().translate(_TRANSLIT))


def empty_row() -> dict:
    return {c: None for c in core.COLS_RU}


def make_sku(r: dict) -> str:
    """Детерминированный SKU из канонических полей строки (не из текста прайса):
    одна и та же позиция из сида и из прайса получает один SKU."""
    cat = r["Категория"]
    mat = core.mat_code(r["Материал"]) if r.get("Материал") else "NA"
    if not re.search(r"[A-Z0-9]", mat):     # кириллический материал («Паронит», «Медь»)
        mat = _slug(r["Материал"])[:12]      # mat_code вернул бы "" -> коллизия SKU
    if cat == "СНП":
        size = str(r["DN / Размер"]).replace('"', "in").replace(" ", "_").replace("/", "-")
        th = r.get("Толщина / Высота")
        fm = "-FM" if core.GROUP_FULLMETAL in str(r["Маркировка_в_прайсе"]) else ""
        return (f"SNP-{STD_CODE[r['Стандарт']]}-{TYPE_LAT[r['Тип_изделия']]}-{size}"
                f"-{mat}-H{th}{fm}")
    return f"{CAT_PREFIX.get(cat, 'ITEM')}-{_slug(r['Маркировка_в_прайсе'])}-{mat}"


def _pressure_cols(pn_text, kgs_lo, kgs_hi):
    return {"PN / Давление": pn_text,
            "Давление_МПа_мин": None if kgs_lo is None else round(kgs_lo / 10.0, 3),
            "Давление_МПа_макс": None if kgs_hi is None else round(kgs_hi / 10.0, 3),
            "Давление_кгс_мин": kgs_lo, "Давление_кгс_макс": kgs_hi}


def snp_row(std, typ, size, material, thickness, dims, price, *, fullmetal=False,
            stock=None, supplier="", source="SEED") -> dict:
    """Каноническая строка СНП в формате рабочего каталога (26 колонок):
    ГОСТ — «СНП-Д-1-1-50-(Ру)», ОСТ — «СНП Д-3-61-(Ру)-(h)», ASME — «СНП Д RF 2" CL300».
    Давление: ГОСТ 10–40 кгс/см², ОСТ 1.6–4.0 МПа, ASME — класс CL300 (в каталоге
    класс хранится в «кгс»-колонках ×10 — историческое кодирование рабочей БД,
    см. core.ANSI_CLASS_TO_KGS; ядро подбора рассчитано именно на него)."""
    r = empty_row()
    r.update({"Категория": "СНП", "Тип_изделия": typ, "Стандарт": std, "Материал": material,
              "Толщина / Высота": thickness, "Основные_размеры": dims, "Цена_без_НДС": price,
              "Остаток": stock, "Поставщик": supplier, "Источник": source,
              "Координата": "synthetic"})
    if std == "ГОСТ":
        ex = GOST_EXEC[typ]
        r.update({"Маркировка_в_прайсе": f"СНП-{typ}-{ex}-{size}-(Ру)", "Исполнение": ex,
                  "Тип_уплотнения": core.SEAL_TYPE_MAP.get(ex),
                  "Совместимые_исполнения_фланца": core.EXECUTION_TO_FLANGE.get(ex),
                  "DN / Размер": str(size)})
        r.update(_pressure_cols("10.0 - 40.0", 10.0, 40.0) if typ in ("Г", "Д")
                 else _pressure_cols("10.0 - 160.0", 10.0, 160.0))
    elif std == "ОСТ":
        r.update({"Маркировка_в_прайсе": f"СНП {typ}-3-{size}-(Ру)-(h)", "Наполнитель": "3",
                  "DN / Размер": str(size)})
        r.update(_pressure_cols("1.6-2.5-4.0", 16.0, 40.0))
    else:
        r.update({"Маркировка_в_прайсе": f'СНП {typ} RF {size} CL300', "DN / Размер": size})
        r.update(_pressure_cols("CL300", 3000.0, 3000.0))
    if fullmetal:
        r["Маркировка_в_прайсе"] += " " + core.GROUP_FULLMETAL
    r["SKU"] = make_sku(r)
    return r


def simple_row(category, product_type, mark, *, material="", price=None, stock=None,
               std="", dn="", pn_text="", thickness=None, dims="", supplier="",
               source="SEED") -> dict:
    """Строка прочих категорий (набивки, прокладки, кольца, ленты)."""
    r = empty_row()
    r.update({"Категория": category, "Тип_изделия": product_type, "Стандарт": std,
              "Маркировка_в_прайсе": mark, "DN / Размер": str(dn), "Материал": material,
              "PN / Давление": pn_text, "Толщина / Высота": thickness, "Основные_размеры": dims,
              "Цена_без_НДС": price, "Остаток": stock, "Поставщик": supplier,
              "Источник": source, "Координата": "synthetic"})
    r["SKU"] = make_sku(r)
    return r

# ============================ НОРМАЛИЗАЦИЯ «ГРЯЗНОГО» ПРАЙСА ============================

# заголовок колонки -> поле (сравнение по началу нормализованного заголовка),
# как parsers.build_colmap в рабочей версии: колонки читаются по смыслу, не по позиции
HEADER_SYNONYMS = {
    "mark": ("маркировка", "наименование", "позиция"),
    "std": ("стандарт", "гост/ост"),
    "dn": ("dn", "ду", "типоразмер"),
    "pn": ("pn", "ру", "давление"),
    "steel": ("сталь", "материал"),
    "thickness": ("толщина", "h,"),
    "dims": ("размеры",),
    "section": ("сечение",),
    "price": ("цена",),
    "stock": ("остаток", "наличие"),
}

_LAT2CYR = str.maketrans({"A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К",
                          "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У"})
_SNP_HEAD_RE = re.compile(r"^\s*[CС][HН][PП]", re.IGNORECASE)
_SNP_TYPE_RE = re.compile(r"СНП[\s\-]*([АБВГДABД])(?![А-ЯA-Z])", re.IGNORECASE)
_SECTION_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[xх×*]\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_INCH_RE = re.compile(r'^\s*(\d+(?:\s+\d/\d)?)\s*(?:"|in|дюйм)', re.IGNORECASE)
_CLASS_RE = re.compile(r"(?:CL|class|класс)\s*(\d{3,4})", re.IGNORECASE)


def _colmap(columns) -> dict:
    out = {}
    for col in columns:
        h = str(col).strip().lower()
        for field, syns in HEADER_SYNONYMS.items():
            if field not in out and any(h.startswith(s) for s in syns):
                out[field] = col
    return out


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() in ("", "-", "—")


def norm_price(v):
    """«1 250,00 руб.» / «1250» / «по запросу» -> float | None (core.clean_num)."""
    if _blank(v):
        return None
    s = re.sub(r"(?i)руб\.?|р\.|₽|rub", "", str(v)).replace(" ", " ").strip()
    return core.clean_num(s)


def norm_material(v):
    """-> (канон по core.MATERIAL_MAP, распознан ли). «08х18н10т», «AISI 316L»,
    «ss304», «316 L», латинские омоглифы в марках ГОСТ — всё к одному канону."""
    if _blank(v):
        return "", True
    s = str(v).strip()
    low = s.lower()
    if low in ("медь", "м1", "м2", "copper"):
        return "Медь", True
    if low in ("ptfe", "фторопласт", "ф-4", "фторопласт-4"):
        return "PTFE", True
    if low.startswith("паронит"):
        return "Паронит", True
    s = re.sub(r"(?i)^(aisi|сталь)\s*", "", s).replace(" ", "").upper()
    if re.fullmatch(r"\d{2}[A-ZА-Я0-9]+", s):          # марка ГОСТ: латиница -> кириллица
        s = s.translate(_LAT2CYR)
    if s.startswith("SS") and s[2:] in core.MATERIAL_MAP:
        s = s[2:]
    canon = core.MATERIAL_MAP.get(s) or core.MATERIAL_MAP.get(s.replace("TI", "Ti"))
    return (canon, True) if canon else (str(v).strip(), False)


def norm_std(v) -> str:
    s = str(v or "").strip().upper()
    if s.startswith(("ГОСТ", "GOST")):
        return "ГОСТ"
    if s.startswith(("ОСТ", "OST")):
        return "ОСТ"
    if "ASME" in s or "ANSI" in s or "B16" in s:
        return "ASME"
    return ""


def norm_section(v) -> str:
    m = _SECTION_RE.search(str(v or ""))
    return f"{m.group(1).replace('.', ',')}х{m.group(2).replace('.', ',')}" if m else ""


def norm_dims(v) -> str:
    """«d1=147; d2=161 d3=181» -> «d1=147.0, d2=161.0, d3=181.0» (формат каталога)."""
    pairs = re.findall(r"\b(d[1-4]|ring)\s*[=:]\s*(\d+(?:[.,]\d+)?)", str(v or ""), re.IGNORECASE)
    return ", ".join(f"{k.lower()}={float(x.replace(',', '.'))}" for k, x in pairs)


def norm_pressure(v, std):
    """-> (текст, кгс_мин, кгс_макс). «1,0-4,0 МПа» -> 10–40 кгс; «10-40 кгс» и
    «PN40» -> кгс как есть; ОСТ без единицы — МПа; «CL300» -> класс ASME ×10."""
    if _blank(v):
        return "", None, None
    s = str(v).strip()
    m = _CLASS_RE.search(s)
    if m:
        cls = int(m.group(1))
        return f"CL{cls}", float(cls * 10), float(cls * 10)
    nums = [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", s)]
    if not nums:
        return s, None, None
    low = s.lower()
    is_mpa = "мпа" in low or (std == "ОСТ" and "кгс" not in low and not low.startswith("pn"))
    k = 10.0 if is_mpa else 1.0
    lo, hi = min(nums) * k, max(nums) * k
    text = core.norm_pressure(s.replace("МПа", "").replace("мпа", "").replace("кгс", "")
                              .replace("PN", "").replace("pn", "")).strip()
    return text, lo, hi


def _snp_from_price(rec, vendor):
    """Строка прайса СНП -> каноническая строка каталога или (None, причина)."""
    mark = str(rec.get("mark") or "").strip()
    mark = _SNP_HEAD_RE.sub("СНП", mark)                 # «CНП» с латинской C -> «СНП»
    m = _SNP_TYPE_RE.search(mark)
    if not m:
        return None, "не распознан тип СНП в маркировке"
    typ = m.group(1).upper().translate(_LAT2CYR)
    std = norm_std(rec.get("std"))
    if not std:
        return None, "не распознан стандарт"
    if _blank(rec.get("dn")):
        return None, "нет DN / типоразмера"
    dn_raw = str(rec.get("dn")).strip()
    if std == "ASME":
        mi = _INCH_RE.match(dn_raw)
        if not mi:
            return None, f"ASME: размер не в дюймах ({dn_raw})"
        size = f'{mi.group(1)}"'
    else:
        size = int(float(dn_raw.replace(",", ".")))
    material, known = norm_material(rec.get("steel"))
    if not material:
        return None, "не указана сталь"
    th = None if _blank(rec.get("thickness")) else float(str(rec["thickness"]).replace(",", "."))
    row = snp_row(std, typ, size, material, th, norm_dims(rec.get("dims")),
                  norm_price(rec.get("price")), fullmetal="навит" in mark.lower(),
                  supplier=vendor, source="PRICE")
    pn_text, lo, hi = norm_pressure(rec.get("pn"), std)
    if lo is not None:
        row.update(_pressure_cols(pn_text, lo, hi))
    return (row, None if known else f"сталь не из справочника: {material}")


def _simple_from_price(rec, vendor):
    mark = re.sub(r"\s+", " ", str(rec.get("mark") or "")).strip()
    if not mark:
        return None, "пустая маркировка"
    up = mark.upper()
    material, known = norm_material(rec.get("steel"))
    price = norm_price(rec.get("price"))
    stock = None if _blank(rec.get("stock")) else int(float(str(rec["stock"]).replace(",", ".")))
    if up.startswith("НАБИВКА"):
        code = re.sub(r"(?i)^набивка\s*", "", mark)
        code = _SECTION_RE.sub("", code).strip(" -")
        code = re.sub(r"([A-Za-zА-Яа-я]+)[\s\-]*(\d)", r"\1 \2", code)   # «AX-101» -> «AX 101»
        section = norm_section(rec.get("section")) or norm_section(mark)
        if not section:
            return None, "у набивки нет сечения"
        china = " (Китай)" if "китай" in mark.lower() else ""
        code = re.sub(r"(?i)\s*\(?китай\)?", "", code).strip()
        return simple_row("Набивки", "Набивка", f"Набивка {code} {section}{china}",
                          price=price, stock=stock, supplier=vendor, source="PRICE"), None
    if up.startswith(("ПРОКЛАДКА", "ШАЙБА")):
        size = norm_section(mark)
        dims = re.sub(r"(\d)\s*[xх×*]\s*(\d)", r"\1х\2", mark)
        dims = re.sub(r"\s+", " ", dims).replace(".", ",")
        return simple_row("Прокладки", "Прокладка", dims if size else mark, material=material,
                          price=price, stock=stock, supplier=vendor, source="PRICE"), \
            (None if known else f"материал не из справочника: {material}")
    return None, "категория строки не распознана"


def load_price_list(path: Path):
    """-> (DataFrame строк каталога, карантин, журнал качества)."""
    rows, quarantine = [], []
    log = {"sheets": 0, "read": 0, "exact_dups": 0, "conflicts": [], "null_price": 0,
           "warnings": []}
    book = pd.read_excel(path, sheet_name=None, dtype=object)
    for sheet, raw in book.items():
        if sheet.lower().startswith("readme"):
            continue
        log["sheets"] += 1
        vendor = re.match(r"(Vendor [A-E])", sheet)
        vendor = vendor.group(1) if vendor else ""
        cmap = _colmap(raw.columns)
        raw = raw.dropna(how="all")
        log["read"] += len(raw)
        before = len(raw)
        raw = raw.drop_duplicates()
        log["exact_dups"] += before - len(raw)
        for idx, src in raw.iterrows():
            rec = {f: src[c] for f, c in cmap.items()}
            is_snp = _SNP_HEAD_RE.match(str(rec.get("mark") or "")) is not None
            row, note = (_snp_from_price if is_snp else _simple_from_price)(rec, vendor)
            excel_row = int(idx) + 2
            if row is None:
                quarantine.append({"sheet": sheet, "row": excel_row, "reason": note,
                                   "raw": json.dumps({k: None if _blank(v) else str(v)
                                                      for k, v in rec.items()},
                                                     ensure_ascii=False)})
                continue
            row["Координата"] = f"{sheet}!{excel_row}"
            if note:
                log["warnings"].append(f"{sheet}!{excel_row}: {note}")
            rows.append(row)
    df = pd.DataFrame(rows, columns=core.COLS_RU)
    # один SKU дважды с разной ценой: побеждает последняя строка прайса
    dup = df[df.duplicated("SKU", keep=False)]
    for sku, grp in dup.groupby("SKU", sort=False):
        log["conflicts"].append((sku, [None if pd.isna(p) else p for p in grp["Цена_без_НДС"]]))
    df = df.drop_duplicates("SKU", keep="last")
    log["null_price"] = int(df["Цена_без_НДС"].isna().sum())
    return df, quarantine, log


def merge_catalog(seed: pd.DataFrame, prices: pd.DataFrame):
    """Прайс свежее сида: цена/остаток существующего SKU обновляются (если в
    прайсе не пусто), новые SKU добавляются. -> (каталог, журнал изменений цен)."""
    cat = seed.set_index("SKU", drop=False)
    updates = []
    new_rows = []
    for _, r in prices.iterrows():
        sku = r["SKU"]
        if sku in cat.index:
            old = cat.at[sku, "Цена_без_НДС"]
            new = r["Цена_без_НДС"]
            if pd.notna(new) and (pd.isna(old) or abs(float(old) - float(new)) > 1e-9):
                updates.append({"sku": sku, "old_price": None if pd.isna(old) else float(old),
                                "new_price": float(new), "vendor": r["Поставщик"]})
                cat.at[sku, "Цена_без_НДС"] = new
            if pd.notna(r["Остаток"]):
                cat.at[sku, "Остаток"] = r["Остаток"]
        else:
            new_rows.append(r)
    out = pd.concat([cat.reset_index(drop=True), pd.DataFrame(new_rows, columns=core.COLS_RU)],
                    ignore_index=True)
    return out.sort_values("SKU", kind="mergesort").reset_index(drop=True), updates, len(new_rows)

# ============================ SQLite ============================

_REAL = {"dn_num", "p_mpa_min", "p_mpa_max", "p_kgs_min", "p_kgs_max", "thickness",
         "weight", "price"}


def write_sqlite(df: pd.DataFrame, updates, quarantine, path: Path):
    """DROP+CREATE+INSERT+3 индекса — контракт docs/DB_SCHEMA.md."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    cols = []
    for c in core.COLS_DB:
        typ = "REAL" if c in _REAL else "INTEGER" if c == "stock" else "TEXT"
        cols.append(f'"{c}" {typ}' + (" NOT NULL" if c in ("sku", "category") else ""))
    db = df.rename(columns=core.RU2DB)[core.COLS_DB].astype(object)
    db = db.where(pd.notna(db), None)
    with conn:
        conn.execute("DROP TABLE IF EXISTS catalog")
        conn.execute(f"CREATE TABLE catalog ({', '.join(cols)})")
        conn.executemany(f"INSERT INTO catalog VALUES ({', '.join('?' * len(core.COLS_DB))})",
                         db.itertuples(index=False, name=None))
        conn.execute('CREATE UNIQUE INDEX idx_catalog_sku ON catalog ("sku")')
        conn.execute('CREATE INDEX idx_catalog_cat_material ON catalog ("category", "material")')
        conn.execute('CREATE INDEX idx_catalog_mark ON catalog ("mark")')
        conn.execute("DROP TABLE IF EXISTS price_updates")
        conn.execute("CREATE TABLE price_updates (sku TEXT, old_price REAL, new_price REAL, "
                     "vendor TEXT)")
        conn.executemany("INSERT INTO price_updates VALUES (:sku, :old_price, :new_price, :vendor)",
                         updates)
        conn.execute("DROP TABLE IF EXISTS quarantine")
        conn.execute("CREATE TABLE quarantine (sheet TEXT, row INTEGER, reason TEXT, raw TEXT)")
        conn.executemany("INSERT INTO quarantine VALUES (:sheet, :row, :reason, :raw)", quarantine)
    return conn


def load_from_sqlite(conn) -> pd.DataFrame:
    """Чтение обратно — как db.load_catalog рабочей версии: DB-имена -> русские,
    DN_num пересчитывается matching.enrich_dn (ASME — по дюймам). Та же функция
    читает каталог в GUI (src/db_sqlite.py)."""
    return db_sqlite.read_catalog(conn)


SQL_SUMMARY = [
    ("Каталог по категориям",
     "SELECT category AS категория, COUNT(*) AS строк, SUM(price IS NULL) AS без_цены, "
     "CAST(ROUND(AVG(price)) AS INTEGER) AS средняя_цена FROM catalog "
     "GROUP BY category ORDER BY строк DESC"),
    ("СНП: покрытие стандартов и конфигураций",
     "SELECT standard AS стандарт, product_type AS тип, COUNT(*) AS строк, "
     "COUNT(DISTINCT material) AS сталей FROM catalog WHERE category = 'СНП' "
     "GROUP BY standard, product_type ORDER BY standard, product_type"),
    ("Изменения цен из прайса (топ-5 по |Δ%|; >50% — аномалия, проверить вручную)",
     "SELECT sku, old_price, new_price, "
     "ROUND(100.0 * (new_price - old_price) / old_price, 1) AS delta_pct, vendor "
     "FROM price_updates WHERE old_price IS NOT NULL "
     "ORDER BY ABS(new_price - old_price) / old_price DESC LIMIT 5"),
    ("Карантин прайса", "SELECT sheet, row, reason FROM quarantine ORDER BY sheet, row"),
]

# ============================ ПОДБОР ============================

SHOW_COLS = ["SKU", "Маркировка_в_прайсе", "Стандарт", "Материал", "Цена_без_НДС"]


def _fmt_rows(df, extra=(), limit=4, indent="      "):
    if df is None or not len(df):
        return []
    cols = [c for c in SHOW_COLS + list(extra) if c in df.columns]
    lines = []
    for _, r in df[cols].head(limit).iterrows():
        bits = []
        for c in cols:
            v = r[c]
            if v is None or (isinstance(v, float) and pd.isna(v)) or v == "":
                v = "—"
            elif c == "Цена_без_НДС":
                v = f"{float(v):,.0f} ₽".replace(",", " ")
            bits.append(str(v))
        lines.append(indent + " | ".join(bits))
    if len(df) > limit:
        lines.append(f"{indent}… ещё {len(df) - limit}")
    return lines


def run_request(df, line, groups):
    """Одна строка заявки: та же развилка маршрутов, что в рабочем GUI/CLI
    (matching.request_route): resolve (маркировка/тип СНП) / criteria
    (DN/PN/сталь/толщина без маркировки) / generic (текстовый поиск)."""
    cust = matching.parse_customer_line(line)
    route = matching.request_route(cust)
    if route == "resolve":
        res = matching.find_snp(df, cust)
    elif route == "criteria":
        res = matching.search_with_criteria(df, line, cust)
    else:
        res = matching.search_generic(df, line)
    exact = matching.sort_for_manager(res) if res is not None and len(res) else df.iloc[0:0]
    out = {"line": line, "route": route, "qty": cust.get("qty") or 1, "exact": exact,
           "analogs": pd.DataFrame(), "diag": "", "nearest": pd.DataFrame(), "suppliers": []}
    if len(exact):
        base = exact.iloc[0]
        parts = []
        for fn in (matching.find_cross_analogs, matching.find_cross_nominal_analogs,
                   matching.find_brand_analogs):
            a = fn(df, base)
            if a is not None and len(a):
                parts.append(a)
        if parts:
            an = pd.concat(parts, ignore_index=True).drop_duplicates("SKU")
            out["analogs"] = an[~an["SKU"].isin(set(exact["SKU"]))]
        else:
            out["diag"] = matching.diagnose_analogs(df, base)
        out["suppliers"] = matching.suppliers_for_frame(exact.head(1), groups)[0][0]
    else:
        if route in ("resolve", "criteria"):
            out["diag"] = matching.diagnose_miss(df, cust)
        else:
            out["diag"] = matching.diagnose_search(df, line)
            out["nearest"] = matching.nearest_variants(df, line, cust, limit=5)
    return out


def print_request(i, r):
    print(f"\n[{i}] «{r['line']}»  маршрут: {r['route']}, кол-во: {r['qty']}")
    ex = r["exact"]
    if len(ex):
        print(f"    точный подбор: {len(ex)} строк(и)")
        for s in _fmt_rows(ex):
            print(s)
        price = ex.iloc[0]["Цена_без_НДС"]
        if pd.notna(price):
            net = float(price) * float(r["qty"])
            print(f"    КП (1-я позиция × {r['qty']}): {net:,.2f} ₽ без НДС, "
                  f"{net * (1 + core.VAT_RATE):,.2f} ₽ с НДС {core.VAT_RATE:.0%}".replace(",", " "))
        print(f"    возможные поставщики: {', '.join(r['suppliers']) or '—'}")
    else:
        print("    точный подбор: 0 — промах")
    if len(r["analogs"]):
        print(f"    аналоги (требуют подтверждения, вне суммы): {len(r['analogs'])}")
        for s in _fmt_rows(r["analogs"], extra=("Причина",)):
            print(s)
    if r["diag"]:
        print("    диагностика: " + str(r["diag"]).replace("\n", "\n      "))
    if len(r["nearest"]):
        print(f"    ближайшие варианты: {len(r['nearest'])}")
        for s in _fmt_rows(r["nearest"], extra=("Уровень", "Причина"), limit=3):
            print(s)


def status_of(r) -> str:
    if len(r["exact"]):
        return "найдено + аналоги" if len(r["analogs"]) else "найдено"
    return "промах → ближайшие" if len(r["nearest"]) else "промах"

# ============================ MAIN ============================


def build():
    seed_json = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    seed = pd.DataFrame(seed_json["items"]).rename(columns=core.DB2RU)[core.COLS_RU]
    print(f"[1/5] сид каталога: {len(seed)} SKU ({SEED_FILE.name})")

    prices, quarantine, log = load_price_list(PRICES_FILE)
    print(f"[2/5] прайс {PRICES_FILE.name}: листов {log['sheets']}, строк {log['read']}, "
          f"точных дублей {log['exact_dups']}, конфликтов SKU {len(log['conflicts'])}, "
          f"в карантин {len(quarantine)}, без цены {log['null_price']}")
    for sku, ps in log["conflicts"]:
        print(f"      конфликт: {sku} — цены {ps} -> взята последняя")
    for w in log["warnings"]:
        print(f"      предупреждение: {w}")

    catalog, updates, n_new = merge_catalog(seed, prices)
    catalog = matching.enrich_dn(catalog)            # DN_num: ГОСТ/ОСТ — число, ASME — по дюймам
    print(f"      слияние: обновлено цен {len(updates)}, новых SKU {n_new}, итого {len(catalog)}")

    conn = write_sqlite(catalog, updates, quarantine, DB_FILE)
    print(f"[3/5] SQLite: {DB_FILE.relative_to(ROOT)} — catalog/price_updates/quarantine, 3 индекса")
    for title, sql in SQL_SUMMARY:
        res = pd.read_sql_query(sql, conn)
        print(f"\n    -- {title}")
        print("    " + res.to_string(index=False).replace("\n", "\n    "))

    df = load_from_sqlite(conn)
    conn.close()
    print(f"\n[4/5] каталог прочитан из SQLite: {len(df)} строк, {df['Категория'].nunique()} категорий")
    return df


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--query", action="append", help="своя строка заявки (можно несколько)")
    args = ap.parse_args(argv)

    df = build()
    groups = core.load_supplier_groups()     # секция supplier_groups из mock_suppliers.json
    requests = args.query or DEMO_REQUESTS
    print(f"\n[5/5] заявки клиентов: {len(requests)}")
    results = [run_request(df, line, groups) for line in requests]
    for i, r in enumerate(results, 1):
        print_request(i, r)

    print("\n=== ИТОГ ===")
    print(f"{'#':>2}  {'маршрут':<9} {'точно':>5} {'аналоги':>7}  статус")
    for i, r in enumerate(results, 1):
        print(f"{i:>2}  {r['route']:<9} {len(r['exact']):>5} {len(r['analogs']):>7}  {status_of(r)}")
    found = sum(1 for r in results if len(r["exact"]))
    with_an = sum(1 for r in results if len(r["analogs"]))
    print(f"найдено: {found}/{len(results)}, с аналогами: {with_an}, "
          f"промахов: {len(results) - found}")


if __name__ == "__main__":
    main()
