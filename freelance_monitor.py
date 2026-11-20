#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Монітор нових замовлень на freelance.ua.

Що робить:
  - читає публічний список замовлень freelance.ua (без ключів і реєстрації);
  - фільтрує за ключовими словами, тегами, бюджетом і свіжістю;
  - запам'ятовує вже показані замовлення в SQLite, тому повторно їх не надсилає;
  - віддає результат у консоль, CSV, HTML і (опційно) у Telegram.

Залежності: тільки стандартна бібліотека Python 3.9+.

Приклади:
  python freelance_monitor.py
  python freelance_monitor.py -q python -q "телеграм бот" --min-budget 900
  python freelance_monitor.py --config freelance_config.example.json --csv out/orders.csv --html out/orders.html
  python freelance_monitor.py --telegram          # бере токен із TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
  python freelance_monitor.py --loop 600 --telegram   # перевіряти кожні 10 хвилин

Для розкладу (кожні 10 хв) на Windows:
  schtasks /create /tn "Freelance Monitor" /tr "python C:\path\freelance_monitor.py --telegram" /sc minute /mo 10
"""

import argparse
import csv
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_URL = "https://freelance.ua/orders/"
DEFAULT_QUERIES = ["python", "парсинг", "парсер", "telegram бот", "автоматизація"]

# Фільтри за замовчуванням — під мою спеціалізацію (Python: парсинг, автоматизація, боти).
DEFAULT_CONFIG = {
    "queries": list(DEFAULT_QUERIES),
    "keywords": [
        "python",
        "парс",
        "скрап",
        "scrap",
        "telegram",
        "телеграм",
        "бот",
        "автоматиз",
        "excel",
        "google sheets",
        "таблиц",
        "selenium",
        "api",
    ],
    "excludes": [],
    "min_budget": 500,
    "max_budget": None,
    "max_age_hours": 72,
    "pages": 1,
    "request_delay": 1.0,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) freelance-monitor/1.0",
}

# Список замовлень і сторінка пошуку мають різну розмітку карток
# (у пошуку <li class=""> без j-order), тому ріжемо сторінку по заголовках.
TITLE_RE = re.compile(r'<header class="l-project-title".*?<a href="([^"]+)"[^>]*>(.*?)</a>', re.S)
CARD_SPAN = 4000
PRICE_RE = re.compile(r'<span class="l-price">\s*(.*?)\s*</span>', re.S)
TAG_RE = re.compile(r'<a[^>]*class="l-tag[^"]*"[^>]*>(.*?)</a>', re.S)
DESC_RE = re.compile(r"<article>\s*<p>(.*?)</p>", re.S)
AGE_RE = re.compile(r'fa fa-clock-o"></i>\s*(.*?)</li>', re.S)
PROPOSALS_RE = re.compile(r"(\d+)\s*пропозиц", re.S)
HOT_RE = re.compile(r'class="highlited-label"')
ID_RE = re.compile(r"/orders/(\d+)-")
TAG_TEXT_RE = re.compile(r"<[^>]+>")


def setup_stdout() -> None:
    """Кирилиця у Windows-консолі та в пайпах без UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def load_config(path: str | None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    return cfg


def clean_text(raw: str) -> str:
    """Прибирає теги та HTML-сутності, згортає пробіли."""
    text = TAG_TEXT_RE.sub(" ", raw or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_budget(raw: str) -> float | None:
    """'900 грн.' -> 900.0, '1 200 грн' -> 1200.0, 'Договірна' -> None."""
    digits = re.sub(r"[^\d]", "", (raw or "").replace("\xa0", " "))
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None


def parse_age_hours(label: str) -> float | None:
    """'щойно', '15 хвилин тому', 'вчора', '3 днів тому' -> години."""
    text = clean_text(label).lower()
    if not text:
        return None
    if "щойно" in text or "секунд" in text:
        return 0.0
    match = re.search(r"(\d+)\s*(хвилин|годин|день|дні|днів|доба|доби|тиждень|тижн)", text)
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        if unit.startswith("хвилин"):
            return value / 60.0
        if unit.startswith("годин"):
            return float(value)
        if unit.startswith("тиж"):
            return value * 24 * 7.0
        return value * 24.0
    if "вчора" in text:
        return 24.0
    if "позавчора" in text:
        return 48.0
    if "сьогодні" in text:
        return 1.0
    return None


def split_cards(page_html: str) -> list[tuple[str, str]]:
    """Ріже сторінку на картки: (текст перед заголовком, текст від заголовка)."""
    page_html = page_html or ""
    titles = list(TITLE_RE.finditer(page_html))
    cards: list[tuple[str, str]] = []
    for index, match in enumerate(titles):
        end = titles[index + 1].start() if index + 1 < len(titles) else len(page_html)
        pre = page_html[max(0, match.start() - 400) : match.start()]  # тут буває мітка «гаряче»
        body = page_html[match.start() : min(end, match.start() + CARD_SPAN)]
        cards.append((pre, body))
    return cards


def parse_orders(page_html: str) -> list[dict]:
    """Розбирає HTML списку замовлень у список словників."""
    orders: list[dict] = []
    for pre, card in split_cards(page_html):
        title_match = TITLE_RE.search(card)
        if not title_match:
            continue
        url = html.unescape(title_match.group(1)).strip()
        id_match = ID_RE.search(url)
        if not id_match:
            continue

        price_match = PRICE_RE.search(card)
        budget_raw = clean_text(price_match.group(1)) if price_match else ""
        desc_match = DESC_RE.search(card)
        age_match = AGE_RE.search(card)
        proposals_match = PROPOSALS_RE.search(card)

        tags = [clean_text(tag) for tag in TAG_RE.findall(card)]
        tags = [tag for tag in tags if tag]

        orders.append(
            {
                "id": id_match.group(1),
                "title": clean_text(title_match.group(2)),
                "url": url,
                "budget_raw": budget_raw,
                "budget": parse_budget(budget_raw),
                "description": clean_text(desc_match.group(1)) if desc_match else "",
                "age_label": clean_text(age_match.group(1)) if age_match else "",
                "age_hours": parse_age_hours(age_match.group(1)) if age_match else None,
                "proposals": int(proposals_match.group(1)) if proposals_match else 0,
                "tags": tags,
                "hot": bool(HOT_RE.search(pre + card[:200])),
            }
        )
    return orders


def build_url(query: str | None, page: int = 1) -> str:
    params: dict[str, str] = {}
    if query:
        params["q"] = query
    if page and page > 1:
        params["page"] = str(page)
    if not params:
        return BASE_URL
    return BASE_URL + "?" + urllib.parse.urlencode(params)


def fetch_page(url: str, user_agent: str, timeout: int = 30) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "uk,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def collect(cfg: dict) -> list[dict]:
    """Обходить усі запити й сторінки, збирає замовлення без дублів."""
    delay = float(cfg.get("request_delay") or 0)
    pages = max(1, int(cfg.get("pages") or 1))
    ua = cfg.get("user_agent") or DEFAULT_CONFIG["user_agent"]
    queries = [q for q in (cfg.get("queries") or []) if str(q).strip()] or [None]

    collected: dict[str, dict] = {}
    for query in queries:
        for page in range(1, pages + 1):
            url = build_url(query, page)
            try:
                page_html = fetch_page(url, ua)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                print(f"[!] Не вдалося прочитати {url}: {exc}", file=sys.stderr)
                continue

            for order in parse_orders(page_html):
                collected.setdefault(order["id"], {**order, "query": query or ""})

            if delay:
                time.sleep(delay)
    return list(collected.values())


def matches(order: dict, cfg: dict) -> bool:
    budget = order.get("budget")
    min_budget = cfg.get("min_budget")
    max_budget = cfg.get("max_budget")
    if min_budget and (budget is None or budget < float(min_budget)):
        return False
    if max_budget and (budget is None or budget > float(max_budget)):
        return False

    max_age = cfg.get("max_age_hours")
    age = order.get("age_hours")
    if max_age and age is not None and age > float(max_age):
        return False

    haystack = " ".join(
        [
            order.get("title") or "",
            order.get("description") or "",
            " ".join(order.get("tags") or []),
        ]
    ).lower()

    excludes = [w.strip().lower() for w in (cfg.get("excludes") or []) if str(w).strip()]
    if any(word in haystack for word in excludes):
        return False

    keywords = [w.strip().lower() for w in (cfg.get("keywords") or []) if str(w).strip()]
    if keywords and not any(word in haystack for word in keywords):
        return False

    return True


class SeenStore:
    """Дедуплікація: кожне замовлення показуємо один раз."""

    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS seen ("
            "  id TEXT PRIMARY KEY,"
            "  title TEXT,"
            "  budget REAL,"
            "  url TEXT,"
            "  first_seen TEXT"
            ")"
        )
        self.conn.commit()

    def is_new(self, order_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM seen WHERE id = ?", (order_id,)).fetchone()
        return row is None

    def remember(self, order: dict) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen (id, title, budget, url, first_seen) VALUES (?, ?, ?, ?, ?)",
            (
                order.get("id") or "",
                order.get("title") or "",
                order.get("budget"),
                order.get("url") or "",
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def format_row(order: dict) -> dict:
    budget = order.get("budget")
    return {
        "id": order.get("id") or "",
        "title": (order.get("title") or "").strip(),
        "budget": f"{budget:,.0f}".replace(",", " ") if budget is not None else (order.get("budget_raw") or ""),
        "tags": ", ".join(order.get("tags") or []),
        "published": order.get("age_label") or "",
        "proposals": str(order.get("proposals") or 0),
        "description": (order.get("description") or "")[:200],
        "url": order.get("url") or "",
    }


def print_rows(rows: list[dict]) -> None:
    for row in rows:
        print(f"\n[{row['id']}] {row['title']}")
        print(f"  Бюджет: {row['budget'] or '—'} | Опубліковано: {row['published'] or '—'} | Пропозицій: {row['proposals']}")
        if row["tags"]:
            print(f"  Теги: {row['tags']}")
        if row["description"]:
            print(f"  {row['description']}")
        print(f"  {row['url']}")


def write_csv(rows: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fields = ["id", "title", "budget", "tags", "published", "proposals", "description", "url"]
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def write_html(rows: list[dict], path: str, cfg: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    filters = []
    if cfg.get("queries"):
        filters.append("запити: " + ", ".join(str(q) for q in cfg["queries"]))
    if cfg.get("keywords"):
        filters.append("ключові слова: " + ", ".join(str(k) for k in cfg["keywords"]))
    if cfg.get("min_budget"):
        filters.append(f"бюджет від {cfg['min_budget']} грн")

    body = "\n".join(
        "<tr>"
        f"<td class=\"num\">{html.escape(r['budget'] or '—')}</td>"
        f"<td><a href=\"{html.escape(r['url'])}\">{html.escape(r['title'])}</a>"
        f"<div class=\"tags\">{html.escape(r['tags'] or '')}</div></td>"
        f"<td>{html.escape(r['published'] or '—')}</td>"
        f"<td class=\"num\">{html.escape(r['proposals'])}</td>"
        "</tr>"
        for r in rows
    ) or "<tr><td colspan=\"4\">Нових замовлень за фільтрами немає.</td></tr>"

    doc = f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8">
<title>Нові замовлення freelance.ua</title>
<style>
 body{{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:32px;color:#1b1b1b}}
 h1{{font-size:20px;margin:0 0 4px}}
 .meta{{color:#666;font-size:13px;margin-bottom:18px}}
 table{{border-collapse:collapse;width:100%;font-size:14px}}
 th,td{{border-bottom:1px solid #e3e3e3;padding:8px 10px;text-align:left;vertical-align:top}}
 th{{background:#f6f7f9;font-weight:600}}
 td.num{{text-align:right;white-space:nowrap}}
 .tags{{color:#777;font-size:12px;margin-top:3px}}
 a{{color:#0a58ca;text-decoration:none}}
</style></head><body>
<h1>Нові замовлення за фільтрами</h1>
<div class="meta">{datetime.now().strftime('%d.%m.%Y %H:%M')} · {html.escape('; '.join(filters) or 'без фільтрів')} · знайдено: {len(rows)}</div>
<table><thead><tr>
<th>Бюджет, грн</th><th>Замовлення</th><th>Опубліковано</th><th>Пропозицій</th>
</tr></thead><tbody>
{body}
</tbody></table>
</body></html>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)


def telegram_text(row: dict) -> str:
    parts = [f"Нове замовлення на freelance.ua", f"{row['title']}"]
    line = f"Бюджет: {row['budget'] or '—'} грн · пропозицій: {row['proposals']}"
    parts.append(line)
    if row["tags"]:
        parts.append(row["tags"])
    parts.append(row["url"])
    return "\n".join(parts)


def send_telegram(rows: list[dict]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[i] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не задані — пропускаю Telegram.")
        return

    for row in rows[:10]:
        data = urllib.parse.urlencode(
            {"chat_id": chat_id, "text": telegram_text(row), "disable_web_page_preview": "false"}
        ).encode()
        try:
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if not payload.get("ok"):
                print(f"[!] Telegram відповів помилкою: {payload}", file=sys.stderr)
        except urllib.error.URLError as exc:
            print(f"[!] Telegram недоступний: {exc}", file=sys.stderr)
            return


def run_once(args: argparse.Namespace, cfg: dict) -> int:
    store = SeenStore(args.db)
    discovered = 0
    rows: list[dict] = []
    try:
        for order in collect(cfg):
            discovered += 1
            if not matches(order, cfg):
                continue
            if not args.all and not store.is_new(order["id"]):
                continue
            rows.append(format_row(order))
            store.remember(order)
    finally:
        store.close()

    print(f"Переглянуто замовлень: {discovered}. Нових за фільтрами: {len(rows)}.")
    print_rows(rows)

    if args.csv and rows:
        write_csv(rows, args.csv)
        print(f"\nCSV: {args.csv}")
    if args.html:
        write_html(rows, args.html, cfg)
        print(f"HTML: {args.html}")
    if args.telegram and rows:
        send_telegram(rows)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Монітор нових замовлень freelance.ua")
    parser.add_argument("--config", help="JSON-файл з фільтрами")
    parser.add_argument("-q", "--query", action="append", default=[], help="пошуковий запит на сайті (можна кілька)")
    parser.add_argument("-k", "--keyword", action="append", default=[], help="ключове слово для фільтра (можна кілька)")
    parser.add_argument("--exclude", action="append", default=[], help="слово-виключення (можна кілька)")
    parser.add_argument("--min-budget", type=float, default=None, help="мінімальний бюджет, грн")
    parser.add_argument("--max-budget", type=float, default=None, help="максимальний бюджет, грн")
    parser.add_argument("--max-age", type=float, default=None, help="максимальний вік замовлення, годин")
    parser.add_argument("--pages", type=int, default=None, help="скільки сторінок читати на запит")
    parser.add_argument("--db", default="state/freelance_seen.db", help="SQLite для дедуплікації")
    parser.add_argument("--csv", help="записати результат у CSV")
    parser.add_argument("--html", help="записати HTML-звіт")
    parser.add_argument("--telegram", action="store_true", help="надіслати в Telegram")
    parser.add_argument("--all", action="store_true", help="показати всі збіги, не лише нові")
    parser.add_argument("--loop", type=int, default=0, help="перевіряти кожні N секунд (0 — один прогін)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_stdout()
    args = parse_args(argv)
    cfg = load_config(args.config)

    if args.query:
        cfg["queries"] = args.query
    if args.keyword:
        cfg["keywords"] = args.keyword
    if args.exclude:
        cfg["excludes"] = args.exclude
    if args.min_budget is not None:
        cfg["min_budget"] = args.min_budget
    if args.max_budget is not None:
        cfg["max_budget"] = args.max_budget
    if args.max_age is not None:
        cfg["max_age_hours"] = args.max_age
    if args.pages:
        cfg["pages"] = args.pages

    if args.loop and args.loop > 0:
        while True:
            run_once(args, cfg)
            print(f"\n[i] Наступна перевірка через {args.loop} с. Ctrl+C — вихід.")
            time.sleep(args.loop)
    return run_once(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
