#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
tools/generate_mock_data.py — детерминированная генерация синтетических данных.

    python tools/generate_mock_data.py

Пишет в data/:
  * mock_catalog_seed.json — 200 SKU в схеме каталога (26 колонок, DB-имена);
  * mock_suppliers.json    — 5 абстрактных поставщиков Vendor A..E, группы
                             «возможные поставщики» (конъюнктивные правила),
                             классы бренд-эквивалентов набивок;
  * mock_prices.xlsx       — «грязный» входящий прайс (~60 строк, 3 листа):
                             омоглифы, разные единицы давления, диапазоны,
                             опечатки в марках стали, дубли, пропуски.

Все значения синтетические: коды набивок (AX/ТМ/КС/Графитон/EXP) выдуманы,
цены сгенерированы формулой со случайным шумом (seed=42). Размеры колец СНП
правдоподобны (порядок величин из открытых стандартов), но подобраны так,
чтобы показать работу алгоритмов: кросс-аналог «посадка» в допуске 1 мм,
«номинал» вне допуска, guard типа А, буква типа ≠ конфигурация у ASME.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import core  # noqa: E402
from build_mock import snp_row, simple_row  # noqa: E402

DATA = ROOT / "data"
RNG = random.Random(42)

MAT_304, MAT_316L, MAT_321 = "SS304", "SS316L", "SS321 (08Х18Н10Т)"
MAT_K = {MAT_304: 1.0, MAT_316L: 1.35, MAT_321: 1.15, "Нержавеющая сталь 08Х13": 0.8}

# ---- физические размеры СНП (d1, d2, d3, d4), мм -----------------------------
GOST = {50: (49.0, 61.0, 77.0, 95.0), 80: (78.0, 90.0, 108.0, 128.0),
        100: (106.0, 127.3, 149.0, 174.5)}
# ОСТ нумерует типоразмер по d2 — те же кольца в допуске 1 мм («посадка»)
OST = {61: (49.4, 61.3, 76.5, 95.6), 90: (78.4, 90.3, 107.5, 128.6),
       127: (106.4, 127.6, 148.5, 175.1)}
# ASME: 2"/3" — тот же номинальный DN, другой физический размер («номинал»),
# 4" — совпадает с ГОСТ DN100 в допуске («посадка» между ГОСТ и ASME)
ASME = {'2"': (55.6, 69.9, 85.9, 104.6), '3"': (81.0, 101.6, 120.7, 136.7),
        '4"': (106.4, 127.0, 149.4, 174.8)}
STD_K = {"ГОСТ": 3.0, "ОСТ": 3.3, "ASME": 4.0}


def _dims(d, keys, ring=None):
    named = dict(zip(("d1", "d2", "d3", "d4"), d))
    parts = [f"{k}={named[k]}" for k in keys]
    if ring is not None:
        parts.append(f"ring={ring}")
    return ", ".join(parts)


def _price(base, k=1.0):
    return float(round(base * k * RNG.uniform(0.9, 1.1)))


def snp_seed():
    rows = []
    keys = {"А": ("d2", "d3"), "Б": ("d2", "d3"), "В": ("d1", "d2", "d3"),
            "Г": ("d2", "d3", "d4"), "Д": ("d1", "d2", "d3", "d4")}
    mats = {"А": [MAT_304, MAT_321], "Б": [MAT_304, MAT_321], "В": [MAT_304, MAT_316L, MAT_321],
            "Г": [MAT_304, MAT_316L, MAT_321], "Д": [MAT_304, MAT_316L, MAT_321]}
    # ГОСТ Р 52376: А/В/Г/Д, у Г/Д — вторая ценовая группа «как навитая часть»
    for typ in ("А", "В", "Г", "Д"):
        for dn, d in GOST.items():
            for mat in mats[typ]:
                ring = 2.0 if typ != "А" else None
                base = d[2] * STD_K["ГОСТ"] * MAT_K[mat]
                rows.append(snp_row("ГОСТ", typ, dn, mat, 2.5, _dims(d, keys[typ], ring),
                                    _price(base), supplier="Vendor B"))
                if typ in ("Г", "Д") and (mat == MAT_316L or dn == 50):
                    rows.append(snp_row("ГОСТ", typ, dn, mat, 2.5, _dims(d, keys[typ], ring),
                                        _price(base, 1.45), fullmetal=True, supplier="Vendor B"))
    # ОСТ 26.260.454: А/Б/В/Г/Д (В — только 2 стали)
    for typ in ("А", "Б", "В", "Г", "Д"):
        for size, d in OST.items():
            for mat in (mats[typ] if typ != "В" else [MAT_304, MAT_316L]):
                rows.append(snp_row("ОСТ", typ, size, mat, 3.2, _dims(d, keys[typ]),
                                    _price(d[2] * STD_K["ОСТ"] * MAT_K[mat]), supplier="Vendor B"))
    # ASME B16.20: у ASME буква «В» — конфигурация OUTER (как «Г»), не INNER
    for typ in ("В", "Г", "Д"):
        k = ("d2", "d3", "d4") if typ in ("В", "Г") else keys["Д"]
        for inch, d in ASME.items():
            for mat in ([MAT_304, MAT_316L] if typ == "В" else [MAT_304, MAT_316L, MAT_321]):
                rows.append(snp_row("ASME", typ, inch, mat, 4.5, _dims(d, k),
                                    _price(d[2] * STD_K["ASME"] * MAT_K[mat]), supplier="Vendor E"))
    return rows


def rings_seed():
    rows = []
    oval = {(dn, lo, hi): (D, b, h) for dn, lo, hi, D, b, h in core.OVAL_RING_DIMS}
    picks = [(50, 6.3, 10), (50, 16, 20), (80, 6.3, 10), (80, 16, 16), (100, 6.3, 16), (100, 20, 20)]
    for dn, lo, hi in picks:
        D, b, h = oval[(dn, lo, hi)]
        pn = f"{lo:g}" if lo == hi else f"{lo:g}-{hi:g}"
        dims = f"(D)={float(D)}, (A)={float(b)}, (H)={float(h)}"
        for mat in ("Нержавеющая сталь 08Х13", MAT_321):
            rows.append(simple_row("Овальные", "Oval", f"1-1-{dn}-({pn.replace('.', ',')})",
                                   std="ГОСТ", dn=dn, pn_text=pn, thickness=float(h), dims=dims,
                                   material=mat, price=_price(D * 9, MAT_K[mat]), supplier="Vendor B"))
            if lo == 6.3 and dn in (50, 80):      # ОСТ — те же кольца (кросс-аналог овальных)
                rows.append(simple_row("Овальные", "Oval", f"1-{dn}-{pn.replace('.', ',')}",
                                       std="ОСТ", dn=dn, pn_text=pn, thickness=float(h), dims=dims,
                                       material=mat, price=_price(D * 9.5, MAT_K[mat]),
                                       supplier="Vendor E"))
    for r_no, od in ((24, 136.5), (31, 149.2), (37, 174.6)):
        rows.append(simple_row("Овальные", "Oval", f"Oval R{r_no}", std="ASME", dn=f"R{r_no}",
                               dims=f"OD={od}, ID={od - 16}, A=11.1", material=MAT_304,
                               price=_price(od * 12), supplier="Vendor E"))
    for dn in (50, 80):
        rows.append(simple_row("Восьмиугольные", "Octagonal", f"{dn}-(6,3-10)", std="ГОСТ", dn=dn,
                               pn_text="6.3-10", thickness=18.0, material="Нержавеющая сталь 08Х13",
                               dims=f"(D1)={dn + 40.0}, (В)=11.0, (S)=18.0",
                               price=_price((dn + 40) * 11), supplier="Vendor B"))
    for r_no in (24, 31):
        rows.append(simple_row("Восьмиугольные", "Octagonal", f"Octagonal R{r_no}", std="ASME",
                               dn=f"R{r_no}", material=MAT_304, price=_price(1900 + r_no * 20),
                               supplier="Vendor E"))
    return rows


# классы эквивалентности набивок: (vendor, код) — все коды выдуманы
BRAND_CLASSES = [
    ("C1", "графит + хлопок (общепром)", 1400,
     [("Vendor A", "AX 101"), ("Vendor B", "ТМ-110"), ("Vendor C", "КС 1100"), ("Vendor D", "Графитон 101")],
     ("6х6", "8х8", "10х10", "12х12")),
    ("C2", "графит + армирование проволокой (пар, высокие t°)", 4500,
     [("Vendor A", "AX 500"), ("Vendor B", "ТМ-500"), ("Vendor C", "КС 3000"), ("Vendor E", "EXP 30")],
     ("8х8", "10х10", "12х12", "14х14")),
    ("C3", "PTFE (химстойкая)", 6200,
     [("Vendor A", "AX 750"), ("Vendor C", "КС 5100"), ("Vendor D", "Графитон 750")],
     ("6х6", "8х8", "10х10", "12х12")),
]
VENDOR_K = {"Vendor A": 1.0, "Vendor B": 0.95, "Vendor C": 1.05, "Vendor D": 0.9, "Vendor E": 1.15}


def packings_seed():
    rows = []
    for _cid, _title, base, members, sections in BRAND_CLASSES:
        for vendor, code in members:
            for sec in sections:
                rows.append(simple_row("Набивки", "Набивка", f"Набивка {code} {sec}",
                                       price=_price(base, VENDOR_K[vendor]),
                                       stock=RNG.choice([None, 0, 12, 25, 40, 75]), supplier=vendor))
    rows.append(simple_row("Набивки", "Набивка", "Набивка AX 500 10х10 (Китай)",
                           price=_price(4500 * 0.6), stock=60, supplier="Vendor A"))
    for code, sec, vendor in (("АП-31", "10х10", "Vendor D"), ("АП-31", "12х12", "Vendor D"),
                              ("ЛС", "8х8", "Vendor C"), ("ХБП", "10х10", "Vendor D")):
        rows.append(simple_row("Набивки", "Набивка", f"Набивка {code} {sec}", price=_price(650),
                               stock=RNG.choice([None, 30, 80]), supplier=vendor))
    return rows


def other_seed():
    rows = []
    for dn in (50, 80, 100, 150):
        rows.append(simple_row("Прокладки", "Прокладка", f"Прокладка А-{dn}-(1,0-1,6)",
                               material="PTFE", price=_price(dn * 4.2), stock=RNG.choice([None, 10, 40]),
                               supplier="Vendor C"))
        rows.append(simple_row("Прокладки", "Прокладка", f"Прокладка А-{dn}-(1,0-2,5)",
                               material="Паронит", price=_price(dn * 1.6), stock=RNG.choice([None, 50]),
                               supplier="Vendor C"))
    for d, D in ((10, 14), (12, 16), (14, 20), (16, 22), (18, 24), (20, 26)):
        rows.append(simple_row("Прокладки", "Прокладка", f"Шайба медная {d}х{D}х1,5",
                               material="Медь", price=_price(D * 1.5), stock=RNG.choice([0, 200, 500]),
                               supplier="Vendor D"))
    for m in ("36х26х5", "44х28х8", "50х36х7", "60х45х8", "70х50х10", "86х60х13", "100х80х10",
              "120х95х12"):
        rows.append(simple_row("Кольца КГН", "Кольцо", f"Кольцо КГФ-Г-С-{m}", price=_price(380),
                               stock=RNG.choice([0, 15, 41, 61]), supplier="Vendor C"))
    for m in ("10х0,5", "20х0,5", "30х1,0"):
        rows.append(simple_row("Ленты", "Лента", f"Лента ЛГ {m} (15м)", price=_price(1650),
                               stock=RNG.choice([None, 5, 20]), supplier="Vendor A"))
    return rows


def build_seed():
    rows = snp_seed() + rings_seed() + packings_seed() + other_seed()
    skus = [r["SKU"] for r in rows]
    assert len(skus) == len(set(skus)), "SKU не уникальны"
    assert len(rows) == 200, f"ожидалось 200 SKU, получено {len(rows)}"
    return rows


def suppliers_doc():
    vendors = [
        {"id": "vendor_a", "name": "Vendor A", "profile": "плетёные набивки, ленты (бренд AX)",
         "lead_time_days": 5, "min_order_rub": 10000},
        {"id": "vendor_b", "name": "Vendor B", "profile": "СНП ГОСТ/ОСТ, кольца RTJ; набивки ТМ",
         "lead_time_days": 10, "min_order_rub": 25000},
        {"id": "vendor_c", "name": "Vendor C", "profile": "неметаллические прокладки, кольца КГН; набивки КС",
         "lead_time_days": 7, "min_order_rub": 15000},
        {"id": "vendor_d", "name": "Vendor D", "profile": "медные шайбы, ГОСТ-набивки; набивки Графитон",
         "lead_time_days": 3, "min_order_rub": 5000},
        {"id": "vendor_e", "name": "Vendor E", "profile": "импортные стандарты ASME, набивки EXP",
         "lead_time_days": 21, "min_order_rub": 50000},
    ]
    g = []

    def group(gid, title, suppliers, **rules):
        g.append({"id": gid, "title": title, "suppliers": suppliers, "rules": rules})

    # эксклюзивное двухуровневое правило СНП: стандарт И сталь (material_tokens / material_not)
    group("snp_gost_ost", "СНП ГОСТ/ОСТ — любая сталь", ["Vendor B", "Vendor C"],
          categories=["СНП"], standards=["ГОСТ", "ОСТ"])
    group("snp_asme_ss", "СНП ASME — нержавеющая 304/316", ["Vendor E", "Vendor B"],
          categories=["СНП"], standards=["ASME"], material_tokens=["304", "316"])
    group("snp_asme_other", "СНП ASME — прочие стали", ["Vendor C"],
          categories=["СНП"], standards=["ASME"], material_not=["304", "316"])
    group("rings_oval_oct", "Кольца овальные и восьмиугольные", ["Vendor B", "Vendor E"],
          categories=["Овальные", "Восьмиугольные"])
    group("pack_ax", "Набивки AX", ["Vendor A"], categories=["Набивки"], mark_tokens=["AX"])
    group("pack_tm", "Набивки ТМ", ["Vendor B"], categories=["Набивки"], mark_tokens=["ТМ-"])
    group("pack_kc", "Набивки КС", ["Vendor C"], categories=["Набивки"], mark_tokens=["КС "])
    group("pack_grafiton", "Набивки Графитон", ["Vendor D"], categories=["Набивки"],
          mark_tokens=["Графитон"])
    group("pack_exp", "Набивки EXP", ["Vendor E"], categories=["Набивки"], mark_tokens=["EXP"])
    group("pack_gost", "Набивки по ГОСТ (АП-31, ЛС, ХБП)", ["Vendor D", "Vendor C"],
          categories=["Набивки"], mark_tokens=["АП-31", "ЛС", "ХБП"])
    group("gaskets_copper", "Шайбы медные", ["Vendor D"], categories=["Прокладки"],
          material_tokens=["Медь"])
    group("gaskets_soft", "Прокладки неметаллические", ["Vendor C", "Vendor D"],
          categories=["Прокладки"], material_not=["Медь"])
    group("kgn_rings", "Кольца КГН", ["Vendor C"], categories=["Кольца КГН"])
    group("tapes", "Ленты уплотнительные", ["Vendor A"], categories=["Ленты"])
    # честный ноль: категории нет в каталоге — группа видна, но ничего не покрывает
    group("fasteners", "Крепёж (категории пока нет в каталоге)", ["Vendor E"], categories=["Крепеж"])

    classes = [{"id": cid, "title": title,
                "members": [{"brand": v, "mark": code} for v, code in members]}
               for cid, title, _b, members, _s in BRAND_CLASSES]
    return {
        "_comment": "Синтетические данные для портфолио. Vendor A..E — абстрактные поставщики.",
        "vendors": vendors,
        # назначение ОДНОГО поставщика колонке «Поставщик» (формат core.SUPPLIER_RULES:
        # [категория, токен маркировки, [поставщик], {опц. material_tokens/material_not}])
        "supplier_rules": [
            ["Набивки", "AX", ["Vendor A"]],
            ["Набивки", "", ["Vendor D"]],
            ["СНП", "", ["Vendor B"]],
            ["Овальные", "", ["Vendor B"]],
            ["Прокладки", "", ["Vendor D"], {"material_tokens": ["Медь"]}],
            ["Прокладки", "", ["Vendor C"], {"material_not": ["Медь"]}],
        ],
        # ВОЗМОЖНЫЕ (альтернативные) поставщики — конъюнкция правил группы
        "supplier_groups": g,
        "brand_equivalents": {"supplier_per_brand": {v["name"]: v["name"] for v in vendors},
                              "classes": classes},
    }


# ---- «грязный» прайс ---------------------------------------------------------

def _messy_steel(mat):
    return RNG.choice({MAT_304: ["ss304", "AISI 304", "304"],
                       MAT_316L: ["AISI 316L", "316 L", "ss316l"],
                       MAT_321: ["08х18н10т", "08X18H10T", "12Х18Н10Т"]}[mat])


def _messy_price(p):
    if p is None:
        return None
    return RNG.choice([f"{p:,.2f} руб.".replace(",", " ").replace(".", ",", 1).replace(" руб,", " руб."),
                       f"{p:.0f}", f"{p:.2f}".replace(".", ","), f"{p:.0f} ₽"])


def price_list(seed):
    snp = [r for r in seed if r["Категория"] == "СНП" and r["Стандарт"] in ("ГОСТ", "ОСТ")]
    snp_rows = []

    def snp_line(std, typ, size, mat, th, dims, price, fullmetal=False, mark_style=None):
        if std == "ГОСТ":
            ex = "1-1" if typ in ("Г", "Д") else "2-3"
            mark = RNG.choice([f"СНП-{typ}-{ex}-{size}", f"CНП-{typ}-{ex}-{size}",
                               f"снп-{typ.lower()}-{ex}-{size}"]) if mark_style is None else mark_style
            std_txt = RNG.choice(["ГОСТ Р 52376-2005", "гост", "ГОСТ"])
            pn = RNG.choice(["1,0-4,0 МПа", "10-40 кгс", "PN10-40"])
        else:
            mark = f"СНП {typ}-3-{size}" if mark_style is None else mark_style
            std_txt = RNG.choice(["ОСТ 26.260.454-99", "ост", "OST"])
            pn = RNG.choice(["1,6-4,0", "1.6-2.5-4.0 МПа"])
        if fullmetal:
            mark += " (ограничитель как навитая часть)"
        dims_txt = "; ".join(p.replace("=", "=").replace(".0", "") for p in dims.split(", "))
        return {"Маркировка": mark, "Стандарт": std_txt, "DN / типоразмер": size, "Давление": pn,
                "Сталь": _messy_steel(mat) if mat in (MAT_304, MAT_316L, MAT_321) else mat,
                "Толщина, мм": str(th).replace(".", ","), "Размеры, мм": dims_txt,
                "Цена без НДС, руб": _messy_price(price)}

    for r in RNG.sample(snp, 18):                       # обновление цен существующих SKU
        size = int(r["DN / Размер"])
        fm = core.GROUP_FULLMETAL in r["Маркировка_в_прайсе"]
        new_price = float(round(r["Цена_без_НДС"] * RNG.uniform(1.03, 1.12)))
        snp_rows.append(snp_line(r["Стандарт"], r["Тип_изделия"], size, r["Материал"],
                                 r["Толщина / Высота"], r["Основные_размеры"], new_price, fm))
    d150 = (150.0, 163.0, 185.0, 208.0)                 # новые позиции DN150
    for typ, mat in (("Д", MAT_316L), ("Д", MAT_321), ("Г", MAT_316L)):
        keys = ("d1", "d2", "d3", "d4") if typ == "Д" else ("d2", "d3", "d4")
        snp_rows.append(snp_line("ГОСТ", typ, 150, mat, 2.5, _dims(d150, keys, 2.0),
                                 _price(d150[2] * 3 * MAT_K[mat])))
    snp_rows += [dict(snp_rows[0]), dict(snp_rows[5])]  # точные дубли строк
    conflict = dict(snp_rows[2])                        # тот же SKU, другая цена
    conflict["Цена без НДС, руб"] = "999"
    snp_rows.append(conflict)
    snp_rows.append(snp_line("ОСТ", "Д", 163, MAT_316L, 3.2, _dims(d150, ("d1", "d2", "d3", "d4")),
                             None))                     # новая позиция без цены
    no_price = snp_line("ГОСТ", "Г", 80, MAT_304, 2.5, _dims(GOST[80], ("d2", "d3", "d4"), 2.0), 1)
    no_price["Цена без НДС, руб"] = "по запросу"
    snp_rows.append(no_price)
    no_dn = snp_line("ГОСТ", "Д", 200, MAT_304, 2.5, "", 4200)
    no_dn["DN / типоразмер"] = None                     # -> карантин
    snp_rows.append(no_dn)
    din = snp_line("ГОСТ", "Д", 65, MAT_304, 2.5, "", 900)
    din["Стандарт"] = "DIN 2690"                        # -> карантин (стандарт не распознан)
    snp_rows.append(din)
    inconel = {"Маркировка": "СНП Д RF 2\"", "Стандарт": "ASME B16.20", "DN / типоразмер": '2"',
               "Давление": "CL300", "Сталь": "Inconel 625", "Толщина, мм": "4,5",
               "Размеры, мм": "d1=55.6; d2=69.9; d3=85.9; d4=104.6",
               "Цена без НДС, руб": "4 870,00 руб."}     # неизвестная сталь -> предупреждение
    snp_rows.append(inconel)

    pack = [r for r in seed if r["Маркировка_в_прайсе"].startswith("Набивка AX")
            and "Китай" not in r["Маркировка_в_прайсе"]]
    pack_rows = []
    for r in RNG.sample(pack, 9):
        code_sec = r["Маркировка_в_прайсе"].replace("Набивка ", "")
        code, sec = code_sec.rsplit(" ", 1)
        name = RNG.choice([f"Набивка {code.replace(' ', '-')}", f"Набивка  {code}",
                           f"Набивка {code.replace('AX', 'АХ')}"])       # кириллические «АХ»
        sec = RNG.choice([sec.replace("х", "x"), sec.replace("х", " х "), sec.replace("х", "*")])
        pack_rows.append({"Наименование": name, "Сечение, мм": sec,
                          "Цена, руб/кг": _messy_price(float(round(r["Цена_без_НДС"] * 1.06))),
                          "Остаток, кг": RNG.choice([None, 10, 35])})
    for sec in ("6х6", "8х8", "10х10"):                 # новый код AX 105
        pack_rows.append({"Наименование": "Набивка AX 105", "Сечение, мм": sec,
                          "Цена, руб/кг": _messy_price(_price(1550)), "Остаток, кг": 20})
    pack_rows.append({"Наименование": "Набивка AX 101 (Китай)", "Сечение, мм": "8x8",
                      "Цена, руб/кг": "890", "Остаток, кг": 100})
    pack_rows.append({"Наименование": "Набивка AX 750", "Сечение, мм": "8х8",
                      "Цена, руб/кг": "по запросу", "Остаток, кг": None})
    pack_rows.append(dict(pack_rows[1]))                # точный дубль
    pack_rows.append({"Наименование": "Набивка AX 500", "Сечение, мм": None,
                      "Цена, руб/кг": "4700", "Остаток, кг": 5})    # нет сечения -> карантин

    gsk_rows = [
        {"Позиция": "Шайба медная 10 x 14 x 1.5", "Материал": "медь", "Цена": "23,50", "Наличие": 400},
        {"Позиция": "Шайба медная 14х20х1,5", "Материал": "М1", "Цена": "33", "Наличие": 150},
        {"Позиция": "Шайба медная 18 х 24 х 1,5", "Материал": "Медь", "Цена": "41 руб.", "Наличие": 0},
        {"Позиция": "Прокладка А-150-(1,0-1,6)", "Материал": "фторопласт", "Цена": "690",
         "Наличие": 12},
        {"Позиция": "Прокладка А-200-(1,0-1,6)", "Материал": "Ф-4", "Цена": "1 120,00", "Наличие": None},
        {"Позиция": "Прокладка А-200-(1,0-2,5)", "Материал": "паронит ПОН-Б", "Цена": "380",
         "Наличие": 30},
        {"Позиция": "Прокладка А-100-(1,0-2,5)", "Материал": "ТРГ-армированный", "Цена": "540",
         "Наличие": 8},                                 # материал не из справочника
        {"Позиция": "Уплотнитель XYZ-17", "Материал": "резина", "Цена": "120", "Наличие": 3},
    ]
    return {"Vendor B — СНП": snp_rows, "Vendor A — набивки": pack_rows,
            "Vendor D — прокладки": gsk_rows}


def main():
    seed = build_seed()
    DATA.mkdir(exist_ok=True)
    items = [{core.RU2DB[k]: v for k, v in r.items()} for r in seed]
    (DATA / "mock_catalog_seed.json").write_text(json.dumps(
        {"_meta": {"description": "Синтетический каталог уплотнений (портфолио)",
                   "rows": len(items), "columns": core.COLS_DB, "generator": "tools/generate_mock_data.py"},
         "items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    (DATA / "mock_suppliers.json").write_text(json.dumps(suppliers_doc(), ensure_ascii=False, indent=2),
                                              encoding="utf-8")
    sheets = price_list(seed)
    with pd.ExcelWriter(DATA / "mock_prices.xlsx", engine="openpyxl") as xw:
        pd.DataFrame({"README": [
            "Синтетический входящий прайс для build_mock.py (не реальные данные).",
            "Намеренные дефекты: латинские омоглифы (CНП, AX/АХ), разные единицы давления",
            "(МПа / кгс / PN / CL), опечатки в марках стали, точные дубли, конфликт цен,",
            "пустые цены («по запросу»), строки без DN/сечения и с нераспознанным стандартом.",
        ]}).to_excel(xw, sheet_name="README", index=False)
        for name, rows in sheets.items():
            pd.DataFrame(rows).to_excel(xw, sheet_name=name, index=False)
    n_price = sum(len(v) for v in sheets.values())
    by_cat = pd.Series([r["Категория"] for r in seed]).value_counts().to_dict()
    print(f"seed: {len(seed)} SKU {by_cat}")
    print(f"prices: {n_price} строк на {len(sheets)} листах")


if __name__ == "__main__":
    main()
