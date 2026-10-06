# -*- coding: utf-8 -*-
"""
src/catalog.py — сборка и обогащение каталога.

Слои ответственности:
  * чтение Excel и маршрутизация листов — здесь (это слой ввода-вывода);
  * парсинг — в parsers.py поверх абстракции TableData;
  * агрегация/обогащение — здесь, на pandas.DataFrame (это уже внутренняя
    модель данных, а не источник).
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

import core
import parsers
import matching
from table_data import PandasTableData


def build_catalog() -> pd.DataFrame:
    all_rows = []
    for f in core.PRICE_FILES:
        path = Path(f)
        if not path.exists():
            print(f"Файл не найден: {f}")
            continue
        src = "PR2" if "2" in path.name else "PR1"
        sheets = pd.read_excel(path, sheet_name=None, header=None)
        for sheet_name, df in sheets.items():
            core.CUR["src"], core.CUR["sheet"] = src, sheet_name
            table = PandasTableData(df)
            name = sheet_name.strip().lower()
            if "реестр" in name or "должник" in name:
                continue
            if "экстрактор" in name:
                all_rows += parsers.parse_extractors(table)
                continue
            if "бх" in name:
                all_rows += parsers.parse_ring_table(
                    table, "БХ", "БХ", "ГОСТ", "BH", r"^БХ")
                continue
            if "восьмиуг" in name:
                st = ("ГОСТ" if "гост" in name else "ОСТ" if "ост" in name
                      else "ASME" if "asme" in name else None)
                if st:
                    pat = (r"^2-\d+-" if st == "ОСТ" else
                           r"^\d+-" if st == "ГОСТ" else r"^Octagonal ")
                    all_rows += parsers.parse_ring_table(
                        table, "Восьмиугольные", "Octagonal", st, "OCT", pat)
                continue
            if "овал" in name:
                st = ("ГОСТ" if "гост" in name else "ОСТ" if "ост" in name
                      else "ASME" if "asme" in name else None)
                if st:
                    pat = (r"^1-\d+-" if st == "ОСТ" else
                           r"^1-1-\d+-" if st == "ГОСТ" else r"^Oval ")
                    all_rows += parsers.parse_ring_table(
                        table, "Овальные", "Oval", st, "OVAL", pat)
                continue
            if "набив" in name:
                all_rows += parsers.parse_packing(table)
                continue
            if "лента" in name or "ме502" in name:
                all_rows += parsers.parse_simple(
                    table, ["лента"], "лента", "цена розн",
                    "Ленты", "Лента", "TAPE")
                if "ме502" in name:
                    all_rows += parsers.parse_me502_pairs(table)
                continue
            if "птфэ" in name or "птфе" in name:
                all_rows += parsers.parse_stock_sheet(table, "ПТФЭ")
                continue
            if "медн" in name:
                all_rows += parsers.parse_copper(table)
                continue
            if "кольца кгн" in name or sheet_name.strip() == "Кольца КГН":
                all_rows += parsers.parse_kgn_rings(table)
                continue                        
            # СНП и СНП ASME ищем по маркировке на любом листе
            try:
                all_rows += parsers.parse_snp_df(table)
                all_rows += parsers.parse_asme_snp_df(table)
            except Exception as e:
                print(f"Лист «{sheet_name}» пропущен (СНП-парсер): {e}")

    df = pd.DataFrame(all_rows)
    df = df.drop_duplicates(subset=["SKU"], keep="first")
    df = enrich(df)
    df = apply_manual_packings(df)
    df = apply_vendor_a_prices(df)
    df = apply_brand_equivalents(df)
    df = apply_supplier_overrides_v4(df)
    df = df.reindex(columns=core.COLS_RU)
    df = matching.enrich_dn(df)
    return df


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    for col in core.COLS_RU:
        if col not in df.columns:
            df[col] = None
    snp = df["Категория"].astype(str).str.upper() == "СНП"
    for idx in df[snp].index:
        if pd.isna(df.at[idx, "Давление_МПа_мин"]):
            lo, hi, klo, khi = core.pressure_range(df.at[idx, "PN / Давление"])
            df.at[idx, "Давление_МПа_мин"], df.at[idx, "Давление_МПа_макс"] = lo, hi
            df.at[idx, "Давление_кгс_мин"], df.at[idx, "Давление_кгс_макс"] = klo, khi
        m = re.search(r"\d+", str(df.at[idx, "DN / Размер"]))
        df.at[idx, "DN_num"] = int(m.group(0)) if m else None
        if df.at[idx, "Исполнение"] in core.EXECUTION_TO_FLANGE:
            df.at[idx, "Совместимые_исполнения_фланца"] = \
                core.EXECUTION_TO_FLANGE[df.at[idx, "Исполнение"]]
    # наполнитель (материал) и тип уплотнения
    for idx in df[snp].index:
        fnum = df.at[idx, "Наполнитель"]
        if pd.notna(fnum):
            try:
                df.at[idx, "Наполнитель_материал"] = core.FILLER_MAP.get(
                    str(int(float(fnum))), "")
            except Exception:
                pass
        ex = df.at[idx, "Исполнение"]
        if pd.notna(ex):
            df.at[idx, "Тип_уплотнения"] = core.SEAL_TYPE_MAP.get(str(ex), "")
    # поставщик (может быть уже задан парсером, например Vendor T)
    for idx in df.index:
        cur = df.at[idx, "Поставщик"]
        if pd.notna(cur) and str(cur).strip():
            continue
        df.at[idx, "Поставщик"] = supplier_for(
            str(df.at[idx, "Категория"]), df.at[idx, "Маркировка_в_прайсе"],
            df.at[idx, "Материал"])
    # автогенерация недостающих исполнений ГОСТ (по флагу; сейчас выключена)
    if core.GENERATE_MISSING_EXECUTIONS:
        extra, seen = [], set()
        gost = snp & (df["Стандарт"] == "ГОСТ")
        for idx in df[gost].index:
            r = df.loc[idx]
            key = (r["Тип_изделия"], r["DN_num"], r["PN / Давление"],
                   r["Материал"], r["Толщина / Высота"])
            if key in seen:
                continue
            seen.add(key)
            have = set(df[gost & (df["Тип_изделия"] == r["Тип_изделия"])
                          & (df["DN_num"] == r["DN_num"])
                          & (df["PN / Давление"].astype(str) == str(r["PN / Давление"]))
                          & (df["Материал"] == r["Материал"])]["Исполнение"].dropna())
            for ex in core.EXECUTIONS_GOST.get(r["Тип_изделия"], []):
                if ex in have or ex == r["Исполнение"]:
                    continue
                n = r.copy()
                old = r["Исполнение"]
                n["Исполнение"] = ex
                n["Совместимые_исполнения_фланца"] = core.EXECUTION_TO_FLANGE.get(ex, "")
                if old:
                    n["Маркировка_в_прайсе"] = str(r["Маркировка_в_прайсе"]).replace(
                        f"-{old}-", f"-{ex}-", 1)
                    n["SKU"] = str(r["SKU"]).replace(f"-{old}-", f"-{ex}-", 1)
                extra.append(n)
        if extra:
            df = pd.concat([df, pd.DataFrame(extra)], ignore_index=True) \
                    .drop_duplicates(subset=["SKU"])
    df = enrich_oval_dims(df)
    return df


def enrich_oval_dims(df: pd.DataFrame) -> pd.DataFrame:
    """Проставляет D/b/h овальным кольцам, у которых в прайсе нет размеров."""
    oval_dims = core.load_oval_dims()
    ov = (df["Категория"] == "Овальные") & \
         (df["Основные_размеры"].astype(str).str.strip().isin(["", "nan"]))
    for idx in df[ov].index:
        dn_m = re.search(r"\d+", str(df.at[idx, "DN / Размер"]))
        if not dn_m:
            continue
        dn = int(dn_m.group(0))
        mark = str(df.at[idx, "Маркировка_в_прайсе"])
        pos = mark.find(f"-{dn}")
        tail = mark[pos + len(str(dn)) + 1:] if pos >= 0 else ""
        vals = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", tail.replace(",", "."))]
        if not vals:
            continue
        lo, hi = min(vals), max(vals)
        for (d, plo, phi, D, b, h) in oval_dims:
            if d == dn and not (hi < plo or phi < lo):
                df.at[idx, "Основные_размеры"] = f"D={D}, b={b}, h={h}"
                df.at[idx, "Толщина / Высота"] = h
                break
    return df


def supplier_for(cat: str, mark, material="") -> str:
    """Возвращает 'Поставщик1; Поставщик2' по категории, маркировке и (итерация
    40) материалу: необязательный 4-й элемент правила — {"material_tokens":
    [...], "material_not": [...]} (подстроки, регистр не важен)."""
    mark_u = str(mark).upper()
    mat_u = "" if material is None or pd.isna(material) else str(material).upper()
    fallback = None
    for rule in core.load_supplier_rules():
        c, marker, sup = rule[0], rule[1], rule[2]
        if c != cat:
            continue
        cond = rule[3] if len(rule) > 3 and rule[3] else {}
        tok = cond.get("material_tokens") or []
        if tok and not any(t.upper() in mat_u for t in tok):
            continue
        if any(t.upper() in mat_u for t in cond.get("material_not") or []):
            continue
        if marker:
            if marker in mark_u:
                return "; ".join(sup)
        elif fallback is None:
            fallback = sup
    return "; ".join(fallback) if fallback else ""


def apply_manual_packings(df):
    """Ручной справочник: доливает цены в позиции без цены;
    добавляет позицию только если бренда вообще нет в каталоге."""
    items, pack_price = core.load_manual_packings()
    for name, buy, ret, wh in items:
        key = core.norm_brand(name)
        mask = (df["Категория"] == "Набивки") & \
               df["Маркировка_в_прайсе"].map(lambda s: core.norm_brand(s).startswith(key))
        if mask.any():
            idx = df.index[mask & df["Цена_без_НДС"].isna()]
            df.loc[idx, "Цена_без_НДС"] = ret if pack_price == "retail" else wh
            df.loc[idx, "Поставщик"] = "Vendor A"
        else:
            price = ret if pack_price == "retail" else wh
            df = pd.concat([df, pd.DataFrame([dict(
                SKU=f"PACKING-{key}",
                Категория="Набивки", Тип_изделия="Набивка", Стандарт="",
                Маркировка_в_прайсе=name,
                **{"DN / Размер": "", "PN / Давление": ""},
                Материал="", **{"Толщина / Высота": None},
                Основные_размеры="", Вес_кг=None,
                Цена_без_НДС=price,
                Поставщик="Vendor A",
                Исполнение=None, Наполнитель=None,
                Источник="MANUAL",
                Координата="фото прайса Vendor A от 23.05.25")])], ignore_index=True)
    return df


def _brand_present(df: pd.DataFrame, marking: str) -> bool:
    """Бренд marking уже встречается в уже собранном каталоге «Набивки» —
    сравнение по ЦЕЛЫМ словам маркировки, не по всей строке одним склеенным
    блоком (первая версия так и делала — nb.str.contains(key) — и поймала
    ложное совпадение в разведке итерации 31: «Гермапрум С-610 16х16»
    (Vendor I) после norm_brand склеивается в «...UMC610...», где хвост
    «...М» слова «ГЕРМАПРУМ» и код «С-610»->«C610» случайно образуют
    подстроку «MC610» — код СОВСЕМ ДРУГОГО бренда (Vendor A, тот же
    класс эквивалентности, но другой код, не дубль одной маркировки).
    Здесь строится список нормализованных ЦЕЛЫХ слов маркировки и проверяется
    совпадение key с любой последовательностью СМЕЖНЫХ целых слов, склеенных
    без разрыва (многословные коды вроде «XTex H 1200» — законный случай,
    ключ обязан охватывать оба слова целиком)."""
    key = core.norm_brand(marking)
    if not key:
        return False
    existing = df.loc[df["Категория"] == "Набивки", "Маркировка_в_прайсе"] \
        .fillna("").astype(str)
    for mark in existing:
        tokens = [t for t in (core.norm_brand(w) for w in re.split(r"\s+", mark)) if t]
        for start in range(len(tokens)):
            joined = ""
            for tok in tokens[start:]:
                joined += tok
                # ТОЛЬКО joined==key или joined с полным key-префиксом —
                # НЕ key.startswith(joined): короткий частичный joined
                # («MC» на полпути к «MC101») иначе ложно «совпадал» бы с
                # ЛЮБЫМ более длинным ключом, начинающимся на те же буквы
                # («MC900», «MC134» — разведка итерации 31 поймала именно
                # это как регресс после первого исправления).
                if joined == key or joined.startswith(key):
                    return True
                if len(joined) > len(key) + 8:
                    break
    return False


def apply_vendor_a_prices(df: pd.DataFrame) -> pd.DataFrame:
    """Прайс Vendor A, май 2025 (parsers.parse_vendor_a_prices,
    src/data/vendor_a_prices_2025-05.json) — доливает ТОЛЬКО бренды,
    отсутствующие в уже собранном каталоге (под любым существующим
    названием — _brand_present); цены уже присутствующих брендов не
    перезаписывает, новых строк для них не создаёт (итерация 31)."""
    new_rows = [r for r in parsers.parse_vendor_a_prices()
                if not _brand_present(df, r["Маркировка_в_прайсе"])]
    if new_rows:
        df = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True) \
                .drop_duplicates(subset=["SKU"], keep="first")
    return df


def apply_brand_equivalents(df: pd.DataFrame) -> pd.DataFrame:
    """Классы эквивалентности брендов (parsers.parse_brand_equivalents,
    src/data/brand_equivalents.json) — доливает строки «Набивки» с ценой
    NULL («цена по запросу») ТОЛЬКО для членов класса, отсутствующих в
    каталоге под любым названием (после apply_vendor_a_prices — «Vendor A»/МС-610
    и т.п. уже покрыты и пропускаются здесь); дубликаты одного кода внутри
    класса под двумя брендами (напр. «НУ-1220» у Vendor C и Vendor B) схлопывает
    финальный drop_duplicates(subset=["SKU"]) — SKU строится из одной и той
    же маркировки (итерация 31)."""
    new_rows = [r for r in parsers.parse_brand_equivalents()
                if not _brand_present(df, r["Маркировка_в_прайсе"])]
    if new_rows:
        df = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True) \
                .drop_duplicates(subset=["SKU"], keep="first")
    return df


def apply_supplier_overrides_v4(df: pd.DataFrame) -> pd.DataFrame:
    """Оверрайды атрибуции поставщиков v4 (итерация 33, источник — бизнес-
    таблица «Наименование товара -> Поставщик», приложена к постановке, плюс
    устные подтверждения бизнеса от 23.09.2026) — применяются ПОСЛЕДНИМИ в
    build_catalog, поверх уже собранных строк (в т.ч. поверх enrich()'а
    SUPPLIER_RULES и apply_manual_packings/apply_vendor_a_prices/
    apply_brand_equivalents), т.к. это осознанные business-исправления
    более ранних, менее точных назначений, а не парсинг новых данных.
    Векторно, без .map()/.apply() на str-колонках (pandas 3.0, CLAUDE.md).

    Кольца КГН (parsers.parse_kgn_rings ставит placeholder «поставщик не
    определён» для всех 19 строк, SUPPLIER_RULES эту категорию не покрывает
    вовсе): по умолчанию «Vendor C»; маркировки с ПУТГ|ТРГ|ТМГ (собственные
    графитовые уплотнения Vendor Bа) -> «Vendor B». Токены ищутся на границе
    слова/дефиса, чтобы не поймать их как случайную подстроку.

    Набивки: бренд «МС»/«MC» целословно (в каталоге код исторически
    встречается ОБОИМИ алфавитами — кириллицей у изначальных строк прайса и
    латиницей у части ручных/brand_equivalents-добавок) -> «Vendor A (Vendor A,
    Vendor A2, Vendor A)»; «Гермапрум»/коды «С-NNN» -> «Vendor I» (эта
    маска применяется ПОСЛЕ МС-маски, чтобы дуально-брендованная строка
    «Гермапрум С-510/МС510» досталась Vendor Iу — намеренно, разведка
    итерации 29/31: это ОДИН товар под двумя взаимозаменяемыми брендами,
    Гермапрум как более специфичный признак побеждает).

    Лист «Лист графитовый МГ140/МГ100 …» (сегодня 1 строка, PR1) — это не
    набивка, а графитовый ЛИСТ; категории «Листы графитовые» в схеме
    каталога не было -> заводится этой строкой: Категория «Листы
    графитовые», Тип_изделия «Лист», Поставщик «Vendor A (Vendor A, Vendor A2,
    Vendor A)» (тот же поставщик, что и графитовые листы по бизнес-
    таблице). ПТФЭ-прокладка с тем же кодом «МГ-140» в маркировке (другая
    категория, другой товар) этим правилом не затрагивается.

    Канонизация «Vendor D»: старый блиц-фолбэк SUPPLIER_RULES при прошлых
    build мог проставить короткую форму — приводим к core.
    SUPPLIER_CANON_ALIASES на случай остаточных строк (идемпотентно)."""
    mk = df["Маркировка_в_прайсе"].fillna("").astype(str).str.upper()

    kgn = df["Категория"] == "Кольца КГН"
    vendor_b_tok = mk.str.contains(r"(?:^|[\s\-])(?:ПУТГ|ТРГ|ТМГ)(?:$|[\s\-/])", regex=True)
    df.loc[kgn & vendor_b_tok, "Поставщик"] = "Vendor B"
    df.loc[kgn & ~vendor_b_tok, "Поставщик"] = "Vendor C"

    nab = df["Категория"] == "Набивки"
    mc_tok = mk.str.contains(r"(?:^|[\s\(])(?:МС|MC)[\s\-]?\d", regex=True)
    df.loc[nab & mc_tok, "Поставщик"] = "Vendor A"

    gr_tok = mk.str.contains("ГЕРМАПРУМ", regex=False) | mk.str.match(r"^С-\d")
    df.loc[nab & gr_tok, "Поставщик"] = "Vendor I"

    sheet = nab & mk.str.contains("ЛИСТ ГРАФИТОВЫЙ", regex=False) & \
        mk.str.contains(r"МГ[\s\-]?140|МГ[\s\-]?100", regex=True)
    df.loc[sheet, "Категория"] = "Листы графитовые"
    df.loc[sheet, "Тип_изделия"] = "Лист"
    df.loc[sheet, "Поставщик"] = "Vendor A"

    # v5 (итерация 35): бренд XTex принадлежит Vendor H независимо от источника
    # строки — прайсовые строки (Price1.xlsx) получали поставщика листа
    # (Vendor D через SUPPLIER_RULES), ни один оверрайд их не покрывал.
    xtex = mk.str.contains("XTEX", regex=False)
    df.loc[xtex, "Поставщик"] = "Vendor H"

    for short, canon in core.SUPPLIER_CANON_ALIASES.items():
        df.loc[df["Поставщик"].fillna("").astype(str) == short, "Поставщик"] = canon
    return df