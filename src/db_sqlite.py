# -*- coding: utf-8 -*-
"""src/db_sqlite.py — чтение каталога из SQLite (портфолио-замена db.py).

В рабочей версии каталог жил в PostgreSQL (db.py, psycopg2). Здесь тот же
контракт таблицы `catalog` (26 колонок, docs/DB_SCHEMA.md) читается из файла
out/sealmatch.sqlite, который пишет build_mock.write_sqlite. Только
стандартная библиотека (sqlite3) + pandas.

Интерфейс для GUI:
  * load_catalog(db_path=None) -> DataFrame с русскими именами колонок
    (core.COLS_RU), DN_num пересчитан matching.enrich_dn — как
    db.load_catalog рабочей версии;
  * load_table(name, db_path=None) — служебные таблицы демо-сборки
    (price_updates, quarantine) для вкладки «Источники»;
  * get_freshness(db_path=None) -> dict — факты о сборке (время, строки,
    категории) для freshness.py.

Файл БД открывается только на чтение (mode=ro): обычный sqlite3.connect на
несуществующем пути молча создал бы пустую БД и GUI показал бы пустой
каталог вместо понятной ошибки «соберите каталог».
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import core
import matching

PROJECT_ROOT = core.ROOT_DIR.parent
DEFAULT_DB_PATH = Path(os.environ.get("SEALMATCH_SQLITE",
                                      PROJECT_ROOT / "out" / "sealmatch.sqlite"))

BUILD_HINT = "соберите каталог: python build_mock.py"


def resolve_path(db_path=None) -> Path:
    return Path(db_path) if db_path else DEFAULT_DB_PATH


def connect_ro(db_path=None) -> sqlite3.Connection:
    """Соединение только на чтение; нет файла -> FileNotFoundError (файл не создаётся)."""
    p = resolve_path(db_path)
    if not p.exists():
        raise FileNotFoundError(f"БД не найдена: {p} — {BUILD_HINT}")
    return sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True)


def read_catalog(conn: sqlite3.Connection) -> pd.DataFrame:
    """Таблица catalog -> DataFrame: DB-имена -> русские, DN_num по
    matching.enrich_dn (ASME — по дюймам). Общая для GUI и build_mock."""
    df = pd.read_sql_query("SELECT * FROM catalog ORDER BY sku", conn)
    df = df.rename(columns=core.DB2RU)
    return matching.enrich_dn(df)


def load_catalog(db_path=None) -> pd.DataFrame:
    conn = connect_ro(db_path)
    try:
        return read_catalog(conn)
    finally:
        conn.close()


def load_table(name: str, db_path=None) -> pd.DataFrame:
    """Служебная таблица целиком; нет БД/таблицы -> пустой DataFrame."""
    if name not in ("price_updates", "quarantine"):
        raise ValueError(f"неизвестная служебная таблица: {name}")
    try:
        conn = connect_ro(db_path)
    except FileNotFoundError:
        return pd.DataFrame()
    try:
        return pd.read_sql_query(f"SELECT * FROM {name}", conn)
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame()
    finally:
        conn.close()


def get_freshness(db_path=None) -> dict:
    """{path, exists, ts (ISO, UTC — время записи файла БД), rows, categories}.
    Нет файла -> exists=False, остальное пусто."""
    p = resolve_path(db_path)
    info = {"path": str(p), "exists": p.exists(), "ts": None, "rows": 0, "categories": {}}
    if not info["exists"]:
        return info
    info["ts"] = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
    try:
        conn = connect_ro(p)
        try:
            cats = conn.execute("SELECT category, COUNT(*) FROM catalog "
                                "GROUP BY category ORDER BY 2 DESC").fetchall()
        finally:
            conn.close()
        info["categories"] = {c: n for c, n in cats}
        info["rows"] = sum(info["categories"].values())
    except sqlite3.Error:
        pass
    return info
