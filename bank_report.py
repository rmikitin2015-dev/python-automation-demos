#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Звіт по виписці банку: CSV -> категорії -> summary + HTML.

Що робить:
  - читає CSV-виписку (monobank / Приват24 / будь-який експорт: дата, опис, сума);
  - сам визначає роздільник (; або ,) і кодування (UTF-8 / Windows-1251);
  - розкладає витрати за категоріями зі звичайних правил у category_rules.json;
  - рахує підсумки за категоріями, топ-мерчантів, середній чек;
  - пише out/report.csv, out/summary.txt і out/report.html.

Залежності: тільки стандартна бібліотека.

Приклади:
  python bank_report.py sample_statement.csv
  python bank_report.py export.csv --rules category_rules.json --out out
"""

import argparse
import csv
import html
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

DEFAULT_RULES = {
    "income_keywords": ["зарплат", "надходження", "переказ від", "return", "повернення"],
    "categories": [
        {"name": "Продукти", "keywords": ["сільпо", "атб", "novus", "фора", "варус", "metro", "маркет", "продукти"]},
        {"name": "Кафе та доставка", "keywords": ["glovo", "bolt food", "uber eats", "кава", "кав'ярня", "ресторан", "піца", "суші"]},
        {"name": "Транспорт", "keywords": ["uber", "bolt", "uklon", "метро", "маршрут", "бензин", "wog", "окко", "парков"]},
        {"name": "Комуналка та зв'язок", "keywords": ["київводоканал", "київтепло", "іне", "kyivstar", "lifecell", "vodafone", "інтернет"]},
        {"name": "Техніка та софт", "keywords": ["rozetka", "comfy", "apple", "google", "openai", "figma", "adobe", "steam", "підписка", "netflix"]},
        {"name": "Оренда", "keywords": ["оренда", "квартир", "rent"]},
        {"name": "Здоров'я", "keywords": ["аптек", "клінік", "стоматолог", "мед"]},
        {"name": "Одяг", "keywords": ["zara", "h&m", "intertop", "одяг", "взуття"]},
        {"name": "Логістика", "keywords": ["нова пошта", "ukrposhta", "meest", "делівері"]},
    ],
}

NUM_RE = re.compile(r"[-+]?\d[\d\s\u00a0.,]*")


def setup_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def read_text(path: str) -> str:
    with open(path, "rb") as fh:
        raw = fh.read()
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_amount(value: str) -> float | None:
    if value is None:
        return None
    cleaned = (
        str(value)
        .replace("\u00a0", "")
        .replace(" ", "")
        .replace("₴", "")
        .replace("UAH", "")
        .replace("грн", "")
        .strip()
    )
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "") if cleaned.rfind(".") > cleaned.rfind(",") else cleaned.replace(".", "").replace(",", ".")
    elif cleaned.count(",") == 1:
        cleaned = cleaned.replace(",", ".")
    cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        match = NUM_RE.search(cleaned)
        return float(match.group().replace(".", "").replace(",", ".")) if match else None


def parse_date(value: str) -> str:
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%d.%m.%y"):
        try:
            return datetime.strptime(value[:19], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return value[:10]


def sniff_rows(text: str) -> list[list[str]]:
    sample = text[:4096]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in row)]


def locate_columns(rows: list[list[str]]) -> tuple[int, int, int, int]:
    """Повертає (row_index_першого_рядка_даних, date_idx, desc_idx, amount_idx)."""
    header = [c.strip().lower() for c in rows[0]] if rows else []

    def find(*names: str) -> int | None:
        for i, cell in enumerate(header):
            if any(name in cell for name in names):
                return i
        return None

    date_idx = find("дата", "date", "time")
    desc_idx = find("опис", "призначення", "description", "details", "memo", "comment")
    amount_idx = find("сума", "amount", "sum", "value")

    if date_idx is None and desc_idx is None and amount_idx is None:
        # Без шапки: припускаємо дата, опис, сума
        return 0, 0, 1, 2

    if amount_idx is None:
        amount_idx = len(header) - 1
    if desc_idx is None:
        desc_idx = 1 if len(header) > 1 else 0
    if date_idx is None:
        date_idx = 0

    return 1, date_idx, desc_idx, amount_idx


def load_rules(path: str | None) -> dict:
    rules = json.loads(json.dumps(DEFAULT_RULES))
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            rules.update(json.load(fh))
    return rules


def categorize(description: str, amount: float, rules: dict) -> str:
    text = (description or "").lower()
    if amount > 0:
        for word in rules.get("income_keywords", []):
            if word.lower() in text:
                return "Надходження"
        return "Інші надходження"
    for category in rules.get("categories", []):
        for word in category.get("keywords", []):
            if word.lower() in text:
                return category["name"]
    return "Інше"


def normalize(rows: list[list[str]], rules: dict) -> list[dict]:
    start, date_idx, desc_idx, amount_idx = locate_columns(rows)
    records = []
    for row in rows[start:]:
        if len(row) <= max(date_idx, desc_idx, amount_idx):
            continue
        amount = parse_amount(row[amount_idx])
        if amount is None:
            continue
        description = (row[desc_idx] or "").strip()
        records.append(
            {
                "date": parse_date(row[date_idx]),
                "description": description,
                "amount": amount,
                "category": categorize(description, amount, rules),
            }
        )
    return records


def bar(share: float, width: int = 28) -> str:
    filled = max(0, min(width, round(share * width)))
    return "█" * filled + "·" * (width - filled)


def build_summary(records: list[dict], source: str) -> tuple[str, dict]:
    expenses = [r for r in records if r["amount"] < 0]
    incomes = [r for r in records if r["amount"] > 0]
    total_out = sum(-r["amount"] for r in expenses)
    total_in = sum(r["amount"] for r in incomes)

    by_category: dict[str, float] = defaultdict(float)
    for rec in expenses:
        by_category[rec["category"]] += -rec["amount"]

    merchants = Counter(r["description"] for r in expenses)
    dates = sorted({r["date"] for r in records if r["date"]})

    lines = [
        "ЗВІТ ПО ВИПИСЦІ",
        f"Джерело: {source}",
        f"Період: {dates[0] if dates else '—'} … {dates[-1] if dates else '—'}",
        f"Транзакцій: {len(records)} (витрат {len(expenses)}, надходжень {len(incomes)})",
        "",
        f"Надходження: {total_in:>12,.2f} UAH".replace(",", " "),
        f"Витрати:     {total_out:>12,.2f} UAH".replace(",", " "),
        f"Сальдо:      {total_in - total_out:>12,.2f} UAH".replace(",", " "),
        "",
        "ВИТРАТИ ЗА КАТЕГОРІЯМИ",
    ]
    for name, value in sorted(by_category.items(), key=lambda kv: -kv[1]):
        share = value / total_out if total_out else 0
        lines.append(
            f"  {name:<24} {value:>11,.2f} UAH  {share*100:>5.1f}%  {bar(share)}".replace(",", " ")
        )

    lines += ["", "ТОП-5 МІСЦЬ ВИТРАТ"]
    for name, count in merchants.most_common(5):
        lines.append(f"  {count}× {name[:60]}")

    if expenses:
        lines += ["", f"Середній чек: {total_out / len(expenses):,.2f} UAH".replace(",", " ")]

    summary = "\n".join(lines)
    stats = {
        "total_in": total_in,
        "total_out": total_out,
        "by_category": dict(by_category),
        "merchants": merchants.most_common(10),
        "count": len(records),
    }
    return summary, stats


def write_csv(records: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date", "description", "amount", "category"], delimiter=";")
        writer.writeheader()
        writer.writerows(records)


def write_html(records: list[dict], stats: dict, path: str, source: str) -> None:
    total_out = stats["total_out"] or 1
    rows_html = []
    for name, value in sorted(stats["by_category"].items(), key=lambda kv: -kv[1]):
        share = value / total_out
        rows_html.append(
            "<tr>"
            f"<td>{html.escape(name)}</td>"
            f"<td class=\"num\">{value:,.2f}</td>".replace(",", " ")
            + f"<td class=\"num\">{share*100:.1f}%</td>"
            f"<td><div class=\"bar\" style=\"width:{max(1, share*100):.1f}%\"></div></td>"
            "</tr>"
        )

    tx_rows = "\n".join(
        f"<tr><td>{html.escape(r['date'])}</td><td>{html.escape(r['description'][:90])}</td>"
        f"<td>{html.escape(r['category'])}</td>"
        f"<td class=\"num {'neg' if r['amount'] < 0 else 'pos'}\">{r['amount']:,.2f}</td></tr>".replace(",", " ")
        for r in records[:300]
    )

    doc = f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><title>Звіт по виписці</title>
<style>
 body{{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:32px;color:#1b1b1b;max-width:900px}}
 h1{{font-size:20px;margin:0 0 2px}} h2{{font-size:15px;margin:28px 0 8px;color:#333}}
 .meta{{color:#666;font-size:13px;margin-bottom:20px}}
 .cards{{display:flex;gap:12px;flex-wrap:wrap}}
 .card{{border:1px solid #e3e3e3;border-radius:10px;padding:12px 16px;min-width:170px}}
 .card b{{display:block;font-size:20px;margin-top:4px}}
 table{{border-collapse:collapse;width:100%;font-size:14px}}
 th,td{{border-bottom:1px solid #eee;padding:7px 10px;text-align:left}}
 th{{background:#f6f7f9}}
 td.num{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}}
 .neg{{color:#b3261e}} .pos{{color:#1a7f37}}
 .bar{{height:10px;background:#0a58ca;border-radius:5px}}
</style></head><body>
<h1>Звіт по виписці</h1>
<div class="meta">{html.escape(source)} · транзакцій: {stats['count']}</div>
<div class="cards">
 <div class="card">Надходження<b>{stats['total_in']:,.2f} UAH</b></div>
 <div class="card">Витрати<b>{stats['total_out']:,.2f} UAH</b></div>
 <div class="card">Сальдо<b>{stats['total_in'] - stats['total_out']:,.2f} UAH</b></div>
</div>
<h2>Витрати за категоріями</h2>
<table><thead><tr><th>Категорія</th><th>Сума, UAH</th><th>Частка</th><th></th></tr></thead>
<tbody>{''.join(rows_html)}</tbody></table>
<h2>Останні транзакції</h2>
<table><thead><tr><th>Дата</th><th>Опис</th><th>Категорія</th><th>Сума</th></tr></thead>
<tbody>{tx_rows}</tbody></table>
</body></html>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)


def main(argv: list[str] | None = None) -> int:
    setup_stdout()
    parser = argparse.ArgumentParser(description="CSV-виписка -> звіт за категоріями")
    parser.add_argument("csv_path", help="файл виписки (CSV)")
    parser.add_argument("--rules", help="JSON з правилами категорій")
    parser.add_argument("--out", default="out", help="папка для звітів")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.csv_path):
        print(f"[!] Немає файлу: {args.csv_path}", file=sys.stderr)
        return 2

    rules = load_rules(args.rules)
    records = normalize(sniff_rows(read_text(args.csv_path)), rules)
    if not records:
        print("[!] Не знайшов жодної транзакції. Перевір колонки дата/опис/сума.", file=sys.stderr)
        return 1

    summary, stats = build_summary(records, os.path.basename(args.csv_path))
    os.makedirs(args.out, exist_ok=True)

    print(summary)

    summary_path = os.path.join(args.out, "summary.txt")
    csv_path = os.path.join(args.out, "report.csv")
    html_path = os.path.join(args.out, "report.html")

    with open(summary_path, "w", encoding="utf-8") as fh:
        fh.write(summary + "\n")
    write_csv(records, csv_path)
    write_html(records, stats, html_path, os.path.basename(args.csv_path))

    print(f"\nГотово: {summary_path}, {csv_path}, {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
