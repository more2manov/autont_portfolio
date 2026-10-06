# -*- coding: utf-8 -*-
"""
src/mark_parser.py — разбор маркировок (O — Open/Closed).

Добавление нового стандарта (DIN, EN, ...) — это новый метод _parse_xxx
и одна строка в цепочке parse(); существующие парсеры не модифицируются.
"""
from __future__ import annotations

import re


class MarkParser:
    """Парсер маркировок СНП."""

    _GOST = re.compile(r"^СНП-\s*([А-Я])-\s*(\d+)-(\d+)-(\d+)")
    _OST = re.compile(r"^СНП\s+([А-Я])-\s*(\d+)-(\d+)")

    def parse(self, mark) -> dict | None:
        """Возвращает {standard, type, execution, filler, dn} или None."""
        if mark is None:
            return None
        s = str(mark).strip()
        if not s:
            return None
        for parser in (self._parse_gost, self._parse_ost, self._parse_asme):
            result = parser(s)
            if result:
                return result
        return None

    def _parse_gost(self, s: str) -> dict | None:
        m = self._GOST.match(s)
        if m:
            return dict(standard="ГОСТ", type=m.group(1),
                        execution=f"{m.group(2)}-{m.group(3)}",
                        filler=None, dn=m.group(4))
        return None

    def _parse_ost(self, s: str) -> dict | None:
        m = self._OST.match(s)
        if m:
            return dict(standard="ОСТ", type=m.group(1),
                        execution=None, filler=m.group(2), dn=m.group(3))
        return None

    def _parse_asme(self, s: str) -> dict | None:
        # У СНП ASME нет единой текстовой маркировки: строка прайса задаётся
        # колонками (тип / фланец / дюймы / класс), её разбирает
        # parse_asme_snp_df. Метод — точка расширения под будущие стандарты.
        return None


_PARSER = MarkParser()


def get_mark_parser() -> MarkParser:
    """Общий экземпляр (состояния нет — можно переиспользовать)."""
    return _PARSER


def parse_mark(mark) -> dict | None:
    """Совместимая функция-обёртка (интерфейс итерации 1)."""
    return _PARSER.parse(mark)