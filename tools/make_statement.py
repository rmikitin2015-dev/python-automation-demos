#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Генератор синтетичної виписки для перевірки швидкості bank_report.py.

Дані вигадані: суми, дати й мерчанти з category_rules.json. Потрібні лише
для того, щоб прогнати звіт на обсязі 5-10 тисяч транзакцій.

    python tools/make_statement.py --rows 7500 --out out/bench_statement.csv
"""

import argparse
import csv
import os
import random
from datetime import date, timedelta

EXPENSES = [
    ("Сільпо Київ", 80, 900, 9),
    ("АТБ-Маркет", 60, 700, 9),
    ("Novus", 150, 1400, 4),
    ("Glovo", 180, 900, 6),
    ("Кав'ярня Aroma Kava", 45, 220, 8),
    ("Uber Trip Kyiv", 70, 420, 7),
    ("Bolt", 60, 350, 5),
    ("WOG", 900, 2600, 3),
    ("Київстар", 200, 500, 2),
    ("Інтернет провайдер", 250, 450, 2),
    ("Rozetka", 300, 9000, 3),
    ("Google", 200, 700, 1),
    ("Оренда квартири", 15000, 18000, 1),
    ("Аптека АНЦ", 100, 900, 4),
    ("Zara", 900, 4500, 2),
    ("Нова пошта", 60, 280, 5),
    ("Стоматологія", 800, 6000, 1),
]

INCOMES = [
    ("Зарплата ТОВ Ромашка", 25000, 45000, 1),
    ("Надходження від клієнта", 3000, 22000, 3),
    ("Повернення коштів Rozetka", 100, 3000, 2),
]


def pick(rng, table):
    names = [row[0] for row in table]
    weights = [row[3] for row in table]
    return rng.choices(table, weights=weights, k=1)[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=7500)
    parser.add_argument("--out", default="out/bench_statement.csv")
    parser.add_argument("--seed", type=int, default=20261122)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    start = date(2023, 11, 1)
    span = (date(2026, 11, 21) - start).days

    rows = []
    for _ in range(args.rows):
        day = start + timedelta(days=rng.randrange(span))
        income = rng.random() < 0.03
        name, low, high, _ = pick(rng, INCOMES if income else EXPENSES)
        amount = rng.uniform(low, high)
        if not income:
            amount = -amount
        rows.append((day, name, round(amount, 2)))

    rows.sort(key=lambda r: r[0])
    folder = os.path.dirname(os.path.abspath(args.out))
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(["Дата", "Опис", "Сума"])
        for day, name, amount in rows:
            writer.writerow([day.strftime("%d.%m.%Y"), name, f"{amount:.2f}"])
    print(f"Готово: {args.out}, транзакцій: {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
