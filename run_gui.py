"""Запуск графического интерфейса портфолио-версии.

    python run_gui.py             # при отсутствии out/sealmatch.sqlite соберёт каталог
    python run_gui.py --rebuild   # пересобрать каталог из data/ и открыть окно

Каталог собирается из синтетических данных скриптом build_mock.py.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "out", "sealmatch.sqlite")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--rebuild" in argv or not os.path.exists(DB_PATH):
        print("Собираю каталог из синтетических данных (build_mock.py)...")
        subprocess.run([sys.executable, os.path.join(ROOT, "build_mock.py")],
                       check=True, cwd=ROOT, stdout=subprocess.DEVNULL)

    # модули src/ импортируют друг друга по короткому имени (import core),
    # как в рабочей версии, поэтому src/ кладётся в sys.path
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import gui

    gui.main(DB_PATH)


if __name__ == "__main__":
    main()
