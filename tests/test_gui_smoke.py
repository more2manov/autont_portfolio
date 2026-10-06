"""Smoke-тесты GUI портфолио-версии.

    python -m pytest tests/test_gui_smoke.py -v

Каталог собирается build_mock.build() во временную папку (out/ не
трогается), журнал заявок перенаправляется туда же. GUI проверяется
in-process: методы вкладок вызываются напрямую, без эмуляции ввода ОС.
Тесты проверяют поведение (что найдено, что видно на экране), а не только
наличие виджетов. Без дисплея (headless CI без Xvfb) GUI-тесты пропускаются.
"""
import contextlib
import io
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import build_mock  # noqa: E402
import core  # noqa: E402
import db_sqlite  # noqa: E402
import freshness  # noqa: E402
import history  # noqa: E402

tk = pytest.importorskip("tkinter")

N_CATALOG = 212          # 200 SKU сида + 12 новых из мок-прайса
N_QUARANTINE = 4


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "sealmatch.sqlite"
    mp = pytest.MonkeyPatch()
    mp.setattr(build_mock, "DB_FILE", path)
    mp.setattr(build_mock, "ROOT", path.parent)       # для relative_to в отчёте
    with contextlib.redirect_stdout(io.StringIO()):
        build_mock.build()
    mp.undo()
    return path


@pytest.fixture(scope="module")
def app(db_path, tmp_path_factory):
    import gui

    mp = pytest.MonkeyPatch()
    mp.setattr(history, "JOURNAL_PATH", tmp_path_factory.mktemp("hist") / "history.json")
    try:
        root = tk.Tk()
    except tk.TclError as e:
        mp.undo()
        pytest.skip(f"нет дисплея для Tk: {e}")
    root.geometry("1280x800+40+40")
    application = gui.App(root, db_path)
    root.update()
    yield application
    root.destroy()
    mp.undo()


# ---------------------------------------------------------------- данные

def test_catalog_loads_from_sqlite(db_path):
    df = db_sqlite.load_catalog(db_path)
    assert list(df.columns) == core.COLS_RU            # контракт 26 колонок
    assert len(df) == N_CATALOG
    assert df["SKU"].is_unique
    assert df["DN_num"].notna().sum() > 0               # enrich_dn отработал
    assert len(db_sqlite.load_table("quarantine", db_path)) == N_QUARANTINE


def test_missing_db_is_reported_not_created(tmp_path):
    missing = tmp_path / "nope.sqlite"
    with pytest.raises(FileNotFoundError):
        db_sqlite.load_catalog(missing)
    assert not missing.exists()                         # mode=ro: пустой файл не создан
    assert freshness.check_freshness(missing)["stale"] is True


def test_freshness_of_fresh_build(db_path):
    meta = db_sqlite.get_freshness(db_path)
    assert meta["exists"] and meta["rows"] == N_CATALOG
    assert freshness.check_freshness(db_path) == {"stale": False, "reason": ""}


# ---------------------------------------------------------------- GUI

def test_window_has_four_tabs_rendered(app):
    nb = app.notebook
    titles = [nb.tab(t, "text") for t in nb.tabs()]
    assert titles == ["Подбор заявок", "Каталог", "Поставщики", "Источники"]
    for tab in (app.order_tab, app.catalog_tab, app.suppliers_tab, app.sources_tab):
        nb.select(tab)
        app.root.update()
        # видимость, а не наличие: вкладка отображена и имеет высоту
        assert tab.winfo_ismapped()
        assert tab.winfo_height() > 300


def test_order_tab_demo_request(app):
    tab = app.order_tab
    app.notebook.select(tab)
    tab.insert_demo()
    app.root.update()
    parents = tab.exact_tree.get_children()
    texts = [tab.exact_tree.item(p, "text") for p in parents]
    # 4 из 5 строк найдены точно, 5-я — промах с ближайшими вариантами
    found = {int(re.match(r"\[(\d+)\]", t).group(1)) for t in texts if "нет точного" not in t}
    assert found == {1, 2, 3, 4}
    assert any(t.startswith("[5]") and "нет точного совпадения" in t for t in texts)
    assert tab.nearest_rows
    assert "Точных позиций" in tab.status.cget("text")
    assert str(tab.btn_why.cget("state")) == "normal"


def test_order_tab_analogs_on_select(app):
    tab = app.order_tab
    if not tab.exact_rows:
        tab.insert_demo()
    # первый вариант строки 1 (СНП ГОСТ Д DN50) — кросс-аналоги ОСТ/ASME
    first = next(iid for iid, (_r, _c, line_no) in tab.exact_rows.items() if line_no == 1)
    tab.exact_tree.selection_set(first)
    app.root.update()
    assert len(tab.analog_tree.get_children()) > 0
    assert tab.status.cget("text").startswith("аналогов:")


def test_catalog_tab_filters(app):
    tab = app.catalog_tab
    tab.reset_filters()
    assert f"строк: {N_CATALOG}" in tab.lbl_page.cget("text")
    tab.cb_cat.set("СНП")
    tab.cb_std.set("ГОСТ")
    tab.apply_filters()
    f = tab.filtered
    assert len(f) > 0
    assert set(f["Категория"]) == {"СНП"} and set(f["Стандарт"]) == {"ГОСТ"}
    assert len(tab.tree.get_children()) == min(len(f), 100)
    tab.reset_filters()


def test_suppliers_tab_synthetic_vendors(app):
    tab = app.suppliers_tab
    assert tab.groups, "группы из data/mock_suppliers.json не загружены"
    vendors = {s for g in tab.groups for s in g["suppliers"]}
    assert vendors and all(re.fullmatch(r"Vendor [A-E]", v) for v in vendors)
    gid = next(g["id"] for g in tab.groups if tab._group_counts[g["id"]] > 0)
    tab.select_group(gid)
    app.root.update()
    assert len(tab.pos_tree.get_children()) == min(tab._group_counts[gid], tab._SHOW_LIMIT)


def test_sources_tab(app):
    tab = app.sources_tab
    labels = [tab.tree.item(i, "text") for i in tab.tree.get_children("prices")]
    data_sheets = [s for s in labels if "служебный" not in s]
    assert len(data_sheets) == 3                        # 3 листа поставщиков + README
    assert all(s.startswith("Vendor ") for s in data_sheets)
    assert len(tab.quar_tree.get_children()) == N_QUARANTINE
    tab.tree.selection_set("seed")
    app.root.update()
    assert len(tab.result_tree.get_children()) == 200
