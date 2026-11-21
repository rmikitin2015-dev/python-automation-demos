#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Одна команда для трьох офлайн-демо: монітор замовлень, монітор тендерів
і пошук компаній. Мережа не використовується, Telegram не надсилається.

Запуск:
    python demo.py                # звіти в out/demo
    python demo.py --out out/demo
"""

import argparse
import os
import sys
import tempfile


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Три офлайн-демо однією командою")
    parser.add_argument("--out", default="out/demo", help="куди писати звіти (типово out/demo)")
    return parser.parse_args(argv)


def steps(out_dir: str, db_dir: str) -> list[tuple[str, object, list[str], tuple[str, ...]]]:
    """Кроки демо: назва, main-функція, аргументи, очікувані файли.

    Імпорт усередині функції, щоб --help не тягнув усі модулі.
    """
    import freelance_monitor
    import prozorro_monitor
    import prospect_finder

    return [
        (
            "freelance_monitor.py",
            freelance_monitor.main,
            [
                "--demo",
                "--all",
                "--db",
                os.path.join(db_dir, "freelance_seen.db"),
                "--csv",
                os.path.join(out_dir, "orders.csv"),
                "--html",
                os.path.join(out_dir, "orders.html"),
            ],
            ("orders.csv", "orders.html"),
        ),
        (
            "prozorro_monitor.py",
            prozorro_monitor.main,
            [
                "--demo",
                "--all",
                "--db",
                os.path.join(db_dir, "prozorro_seen.db"),
                "--csv",
                os.path.join(out_dir, "tenders.csv"),
                "--html",
                os.path.join(out_dir, "tenders.html"),
            ],
            ("tenders.csv", "tenders.html"),
        ),
        (
            "prospect_finder.py",
            prospect_finder.main,
            ["--demo", "--out", os.path.join(out_dir, "prospects")],
            ("prospects.csv", "prospects.html"),
        ),
    ]


def run(out_dir: str) -> int:
    os.makedirs(out_dir, exist_ok=True)
    failures: list[str] = []

    # Бази дедуплікації — у тимчасовій папці: демо з --all не залежить від історії,
    # а в репозиторії не зʼявляється зайвих файлів.
    with tempfile.TemporaryDirectory(prefix="demo-db-") as db_dir:
        for name, func, argv, expected in steps(out_dir, db_dir):
            print(f"\n=== {name} ===")
            code = func(argv)
            missing = [f for f in expected if not _nonempty(os.path.join(out_dir, f))]
            if code != 0 or missing:
                reason = f"код {code}" if code != 0 else "немає файлів: " + ", ".join(missing)
                failures.append(f"{name}: {reason}")
                print(f"[!] {name}: {reason}", file=sys.stderr)

    total = 3
    print(f"\nГотово: {total - len(failures)} з {total}. Звіти в {out_dir}/")
    for name in sorted(os.listdir(out_dir)):
        size = os.path.getsize(os.path.join(out_dir, name))
        print(f"  {name:<16} {size:>7} байт")

    if failures:
        for line in failures:
            print(f"[!] {line}", file=sys.stderr)
        return 1
    return 0


def _nonempty(path: str) -> bool:
    return os.path.isfile(path) and os.path.getsize(path) > 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run(args.out)


if __name__ == "__main__":
    raise SystemExit(main())
