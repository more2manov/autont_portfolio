# -*- coding: utf-8 -*-
"""src/freshness.py — свежесть собранного каталога (портфолио-версия).

В рабочей версии сборка фиксировала sha256 исходных прайсов в
build_meta.json, а GUI показывал баннер, если прайс правили после сборки
или сборка старше core.STALE_DAYS дней. Здесь тот же интерфейс
(read_meta / format_ts / check_freshness) поверх файла SQLite
(db_sqlite.get_freshness): каталог «устарел», если входные данные
(data/mock_catalog_seed.json, data/mock_prices.xlsx) изменены после
записи out/sealmatch.sqlite. Возраст сборки в демо не учитывается:
синтетический прайс сам по себе не обновляется.
"""
from __future__ import annotations

from datetime import datetime

import core
import db_sqlite

INPUT_FILES = ("mock_catalog_seed.json", "mock_prices.xlsx")


def read_meta(db_path=None) -> dict | None:
    """Факты о сборке (db_sqlite.get_freshness) или None, если БД нет."""
    info = db_sqlite.get_freshness(db_path)
    return info if info["exists"] else None


def format_ts(ts) -> str:
    """ISO-таймстамп -> «ДД.ММ.ГГГГ ЧЧ:ММ» (локальное время); пусто -> заглушка."""
    if not ts:
        return "нет данных"
    try:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return str(ts)


def check_freshness(db_path=None) -> dict:
    """{stale: bool, reason: str}: stale, если БД нет или входной файл новее БД."""
    meta = read_meta(db_path)
    if meta is None:
        return {"stale": True, "reason": "каталог не собран (нет файла SQLite)"}
    built = datetime.fromisoformat(meta["ts"]).timestamp()
    for name in INPUT_FILES:
        p = core.DATA_DIR / name
        if p.exists() and p.stat().st_mtime > built + 1:
            return {"stale": True, "reason": f"{name} изменён после сборки каталога"}
    return {"stale": False, "reason": ""}
