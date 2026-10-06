# -*- coding: utf-8 -*-
"""src/history.py — журнал заявок менеджера (итерация 15).

Headless-safe: не импортирует tkinter — CLI и gui.py (GUI) используют эти
функции напрямую, без обёрток. Журнал — плоский JSON-список в
out/request_history.json (портфолио: рядом с собранной БД, в .gitignore),
append-only: append_entry всегда
дописывает новую запись, mark_kp находит и правит kp_file у уже
существующей (используется, когда КП экспортируется уже ПОСЛЕ подбора).

Формат записи: {ts, lines, hits, misses, kp_file}. lines — строки заявки
как введены (список); hits/misses — счётчики (сколько строк заявки нашли
хоть один вариант / не нашли ни одного), не сами найденные позиции — журнал
фиксирует ФАКТ обращения и его исход, а не дублирует подбор.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import core

JOURNAL_PATH = core.ROOT_DIR.parent / "out" / "request_history.json"


def _bak_path():
    return JOURNAL_PATH.parent / (JOURNAL_PATH.name + ".bak")


def _load_journal() -> list:
    """Список записей из JOURNAL_PATH. Файла нет -> []. Битый JSON (не
    парсится, или парсится не в список) -> переименовать в .bak (без
    исключения наружу, даже если .bak уже существует от прошлой порчи —
    перезаписывается) и начать пустой журнал."""
    if not JOURNAL_PATH.exists():
        return []
    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("request_history.json: верхний уровень не список")
        return data
    except Exception:
        try:
            JOURNAL_PATH.replace(_bak_path())
        except OSError:
            pass
        return []


def _save_journal(entries: list) -> None:
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def append_entry(lines, hits, misses) -> dict:
    """Дописывает запись {ts, lines, hits, misses, kp_file=None} и
    возвращает её (созданный dict, с уже проставленным ts)."""
    entries = _load_journal()
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "lines": list(lines),
        "hits": int(hits),
        "misses": int(misses),
        "kp_file": None,
    }
    entries.append(entry)
    _save_journal(entries)
    return entry


def read_journal(last_n: int = 10) -> list:
    """Последние last_n записей в хронологическом порядке (старые -> новые,
    как лежат в файле). last_n <= 0 -> весь журнал."""
    entries = _load_journal()
    if last_n is None or last_n <= 0:
        return entries
    return entries[-last_n:]


def mark_kp(lines, kp_filename) -> bool:
    """Ставит kp_file=kp_filename ПОСЛЕДНЕЙ записи с ТЕМ ЖЕ списком lines
    (порядок важен — тот же подбор, что породил КП). Возвращает True, если
    такая запись найдена и обновлена, иначе False (например, КП собрали не
    сразу после «Подобрать», а после правки поля заявок)."""
    entries = _load_journal()
    target = list(lines)
    for entry in reversed(entries):
        if entry.get("lines") == target:
            entry["kp_file"] = kp_filename
            _save_journal(entries)
            return True
    return False
