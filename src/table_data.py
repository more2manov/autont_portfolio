# -*- coding: utf-8 -*-
"""
src/table_data.py — абстракция табличных данных (D — Dependency Inversion).

Парсеры зависят от протокола TableData, а не от pandas.DataFrame:
  * парсеры тестируются на простых списках без Excel и pandas;
  * источник данных можно заменить (CSV, SQL, API внешней CMS) новым адаптером,
    не трогая парсеры;
  * при переносе ядра в PHP-плагин контракты остаются теми же — меняется
    только адаптер.
"""
from __future__ import annotations

from typing import Iterator, List, Protocol, Tuple, runtime_checkable

import pandas as pd


@runtime_checkable
class TableData(Protocol):
    """Контракт «таблица только для чтения»."""

    def get_cell(self, i: int, j: int) -> str:
        """Значение ячейки (i, j) строкой; отсутствующие/NaN -> ''."""

    def get_shape(self) -> Tuple[int, int]:
        """(строки, столбцы)."""

    def iter_rows(self, start: int = 0, end: int | None = None
                  ) -> Iterator[Tuple[int, List[str]]]:
        """Итератор: (номер строки, список строковых значений)."""


class PandasTableData:
    """Адаптер pandas.DataFrame к TableData."""

    __slots__ = ("df",)

    def __init__(self, df: pd.DataFrame):
        self.df = df

    def get_cell(self, i: int, j: int) -> str:
        if i < 0 or j < 0 or i >= self.df.shape[0] or j >= self.df.shape[1]:
            return ""
        v = self.df.iat[i, j]
        return str(v).strip() if pd.notna(v) else ""

    def get_shape(self) -> Tuple[int, int]:
        return self.df.shape

    def iter_rows(self, start: int = 0, end: int | None = None):
        nrows, ncols = self.df.shape
        stop = nrows if end is None else min(end, nrows)
        for i in range(start, stop):
            yield i, [self.get_cell(i, j) for j in range(ncols)]


def as_table(src) -> TableData:
    """Принимает TableData либо pandas.DataFrame, всегда возвращает TableData.

    Это «мягкая» точка входа: существующий код (тесты, каталог) может
    передавать DataFrame, а новые модули — сразу абстракцию.
    """
    if isinstance(src, TableData):
        return src
    return PandasTableData(src)