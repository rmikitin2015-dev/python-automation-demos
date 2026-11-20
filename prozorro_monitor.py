#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Монітор тендерів Prozorro.

Що робить:
  - читає відкритий API Prozorro (без ключа, без реєстрації);
  - фільтрує нові закупівлі за ключовими словами, кодом ДК 021, сумою, регіоном;
  - запам'ятовує вже показані тендери в SQLite, тому повторно їх не надсилає;
  - віддає результат у консоль, CSV, HTML-звіт і (опційно) у Telegram.

Залежності: тільки стандартна бібліотека Python 3.9+.

Приклади:
  python prozorro_monitor.py
  python prozorro_monitor.py --config config.example.json --csv out/tenders.csv --html out/tenders.html
  python prozorro_monitor.py -k вентиляц -k "ремонт даху" --min-amount 100000 --region Київ
  python prozorro_monitor.py --telegram            # бере токен із TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID

Для розкладу (кожні 30 хв) на Windows:
  schtasks /create /tn "Prozorro Monitor" /tr "python C:\path\prozorro_monitor.py --telegram" /sc minute /mo 30
"""

import argparse
import csv
import html
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API_URL = "https://public-api.prozorro.gov.ua/api/2.5/tenders"
# Фід віддає лише id + dateModified, тому картку кожного тендера добираємо окремо,
# і просимо тільки потрібні поля (opt_fields) — відповідь у рази легша.
DETAIL_FIELDS = "title,status,value,tenderID,items,procuringEntity,tenderPeriod,dateModified"

DEFAULT_CONFIG = {
    "keywords": [],
    "cpv_prefixes": [],
    "min_amount": 0,
    "max_amount": None,
    "region": None,
    "statuses": ["active.tendering"],
    "max_pages": 3,
    "page_limit": 100,
    "max_inspect": 100,
    "request_delay": 0.15,
}


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


def fetch_json(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "prozorro-monitor/1.0 (freelance demo)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_tender(tender_id: str, timeout: int = 30) -> dict | None:
    url = f"{API_URL}/{tender_id}?{urllib.parse.urlencode({'opt_fields': DETAIL_FIELDS})}"
    try:
        return (fetch_json(url, timeout=timeout) or {}).get("data")
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            return None  # закрита або видалена закупівля
        print(f"[!] Тендер {tender_id}: HTTP {exc.code}", file=sys.stderr)
        return None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"[!] Тендер {tender_id}: {exc}", file=sys.stderr)
        return None


def iter_tenders(cfg: dict, max_age_days: int = 60):
    """Гортує фід посторінково і добирає картки тендерів, поки не вичерпає max_inspect."""
    limit = int(cfg.get("page_limit") or 100)
    max_pages = max(1, int(cfg.get("max_pages") or 1))
    max_inspect = max(1, int(cfg.get("max_inspect") or 100))
    delay = float(cfg.get("request_delay") or 0)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    offset = None
    inspected = 0

    for _ in range(max_pages):
        query = {"descending": "1", "limit": limit}
        if offset:
            query["offset"] = offset
        url = f"{API_URL}?{urllib.parse.urlencode(query)}"

        try:
            payload = fetch_json(url)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"[!] Не вдалося прочитати API: {exc}", file=sys.stderr)
            return

        batch = payload.get("data") or []
        if not batch:
            return

        for ref in batch:
            tender_id = ref.get("id")
            if not tender_id:
                continue
            tender = fetch_tender(tender_id)
            inspected += 1
            if tender:
                yield tender
            if inspected >= max_inspect:
                return
            if delay:
                time.sleep(delay)

        next_page = payload.get("next_page") or {}
        offset = next_page.get("offset")
        if not offset:
            return

        modified = batch[-1].get("dateModified")
        if modified:
            try:
                stamp = datetime.fromisoformat(modified.replace("Z", "+00:00"))
                if stamp < cutoff:
                    return
            except ValueError:
                pass

        time.sleep(1.0)


FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def demo_tenders() -> list[dict]:
    """Тендери зі збереженого знімка API — ті самі картки, але без мережі."""
    path = os.path.join(FIXTURES_DIR, "prozorro_tenders.json")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def tender_amount(tender: dict) -> float | None:
    value = tender.get("value") or {}
    amount = value.get("amount")
    try:
        return float(amount)
    except (TypeError, ValueError):
        return None


def tender_cpv(tender: dict) -> list[str]:
    codes = []
    for item in tender.get("items") or []:
        cls = item.get("classification") or {}
        code = cls.get("id")
        if code and code not in codes:
            codes.append(code)
    return codes


def tender_region(tender: dict) -> str:
    regions = []
    for item in tender.get("items") or []:
        address = item.get("deliveryAddress") or {}
        region = (address.get("region") or "").strip()
        if region and region not in regions:
            regions.append(region)
    return ", ".join(regions)


def tender_deadline(tender: dict) -> str:
    period = tender.get("tenderPeriod") or {}
    raw = period.get("endDate") or ""
    if not raw:
        return ""
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return stamp.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return raw[:16].replace("T", " ")


def matches(tender: dict, cfg: dict) -> bool:
    statuses = cfg.get("statuses") or []
    if statuses and tender.get("status") not in statuses:
        return False

    amount = tender_amount(tender)
    min_amount = cfg.get("min_amount") or 0
    max_amount = cfg.get("max_amount")
    if min_amount and (amount is None or amount < float(min_amount)):
        return False
    if max_amount and (amount is None or amount > float(max_amount)):
        return False

    prefixes = [p for p in (cfg.get("cpv_prefixes") or []) if p]
    if prefixes:
        codes = tender_cpv(tender)
        if not any(code.startswith(tuple(prefixes)) for code in codes):
            return False

    region = (cfg.get("region") or "").strip().lower()
    if region and region not in tender_region(tender).lower():
        return False

    keywords = [k.strip().lower() for k in (cfg.get("keywords") or []) if k.strip()]
    if keywords:
        haystack = " ".join(
            [
                tender.get("title") or "",
                tender.get("description") or "",
                (tender.get("procuringEntity") or {}).get("name") or "",
                " ".join(item.get("description") or "" for item in tender.get("items") or []),
            ]
        ).lower()
        if not any(word in haystack for word in keywords):
            return False

    return True


class SeenStore:
    """Дедуплікація: тендер показуємо один раз."""

    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS seen ("
            "  id TEXT PRIMARY KEY,"
            "  tender_id TEXT,"
            "  title TEXT,"
            "  amount REAL,"
            "  first_seen TEXT"
            ")"
        )
        self.conn.commit()

    def is_new(self, tender_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM seen WHERE id = ?", (tender_id,)).fetchone()
        return row is None

    def remember(self, tender: dict) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen (id, tender_id, title, amount, first_seen) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                tender.get("id") or "",
                tender.get("tenderID") or "",
                tender.get("title") or "",
                tender_amount(tender),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def format_row(tender: dict) -> dict:
    amount = tender_amount(tender)
    tender_id = tender.get("tenderID") or tender.get("id") or ""
    return {
        "tender_id": tender_id,
        "title": (tender.get("title") or "").strip(),
        "amount": f"{amount:,.0f}".replace(",", " ") if amount is not None else "",
        "buyer": (tender.get("procuringEntity") or {}).get("name") or "",
        "cpv": ", ".join(tender_cpv(tender)),
        "region": tender_region(tender),
        "deadline": tender_deadline(tender),
        "url": f"https://prozorro.gov.ua/tender/{tender_id}",
    }


def print_rows(rows: list[dict]) -> None:
    for row in rows:
        print(f"\n[{row['tender_id']}] {row['title']}")
        print(f"  Сума: {row['amount'] or '—'} UAH | Дедлайн: {row['deadline'] or '—'}")
        print(f"  Замовник: {row['buyer'] or '—'}")
        print(f"  ДК 021: {row['cpv'] or '—'} | Регіон: {row['region'] or '—'}")
        print(f"  {row['url']}")


def write_csv(rows: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["tender_id", "title", "amount", "buyer", "cpv", "region", "deadline", "url"],
            delimiter=";",
        )
        writer.writeheader()
        writer.writerows(rows)


def write_html(rows: list[dict], path: str, cfg: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    filters = []
    if cfg.get("keywords"):
        filters.append("ключові слова: " + ", ".join(cfg["keywords"]))
    if cfg.get("cpv_prefixes"):
        filters.append("ДК 021: " + ", ".join(cfg["cpv_prefixes"]))
    if cfg.get("min_amount"):
        filters.append(f"від {cfg['min_amount']} UAH")
    if cfg.get("region"):
        filters.append("регіон: " + str(cfg["region"]))

    body = "\n".join(
        "<tr>"
        f"<td><a href=\"{html.escape(r['url'])}\">{html.escape(r['tender_id'])}</a></td>"
        f"<td>{html.escape(r['title'])}</td>"
        f"<td class=\"num\">{html.escape(r['amount'] or '—')}</td>"
        f"<td>{html.escape(r['deadline'] or '—')}</td>"
        f"<td>{html.escape(r['region'] or '—')}</td>"
        f"<td>{html.escape(r['buyer'] or '—')}</td>"
        "</tr>"
        for r in rows
    ) or "<tr><td colspan=\"6\">Нових тендерів за фільтрами немає.</td></tr>"

    doc = f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8">
<title>Нові тендери Prozorro</title>
<style>
 body{{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:32px;color:#1b1b1b}}
 h1{{font-size:20px;margin:0 0 4px}}
 .meta{{color:#666;font-size:13px;margin-bottom:18px}}
 table{{border-collapse:collapse;width:100%;font-size:14px}}
 th,td{{border-bottom:1px solid #e3e3e3;padding:8px 10px;text-align:left;vertical-align:top}}
 th{{background:#f6f7f9;font-weight:600}}
 td.num{{text-align:right;white-space:nowrap}}
 a{{color:#0a58ca;text-decoration:none}}
</style></head><body>
<h1>Нові тендери за вашими фільтрами</h1>
<div class="meta">{datetime.now().strftime('%d.%m.%Y %H:%M')} · {html.escape('; '.join(filters) or 'без фільтрів')} · знайдено: {len(rows)}</div>
<table><thead><tr>
<th>Номер</th><th>Назва</th><th>Сума, UAH</th><th>Подача до</th><th>Регіон</th><th>Замовник</th>
</tr></thead><tbody>
{body}
</tbody></table>
</body></html>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)


def send_telegram(rows: list[dict]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[i] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не задані — пропускаю Telegram.")
        return

    header = f"Нові тендери: {len(rows)}"
    chunks = [header]
    for row in rows[:15]:
        chunks.append(
            f"\n{row['title'][:90]}\n{row['amount'] or '—'} UAH · до {row['deadline'] or '—'}\n{row['url']}"
        )
    text = "\n".join(chunks)

    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if not payload.get("ok"):
            print(f"[!] Telegram відповів помилкою: {payload}", file=sys.stderr)
    except urllib.error.URLError as exc:
        print(f"[!] Telegram недоступний: {exc}", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Монітор нових тендерів Prozorro")
    parser.add_argument("--config", help="JSON-файл з фільтрами")
    parser.add_argument("-k", "--keyword", action="append", default=[], help="ключове слово (можна кілька)")
    parser.add_argument("--cpv", action="append", default=[], help="префікс коду ДК 021, напр. 45")
    parser.add_argument("--min-amount", type=float, default=None, help="мінімальна сума, UAH")
    parser.add_argument("--max-amount", type=float, default=None, help="максимальна сума, UAH")
    parser.add_argument("--region", help="частина назви області, напр. Київ")
    parser.add_argument("--status", action="append", default=[], help="статус (типово active.tendering)")
    parser.add_argument("--max-pages", type=int, default=None, help="скільки сторінок фіду читати")
    parser.add_argument("--inspect", type=int, default=None, help="скільки карток тендерів завантажити за прогін")
    parser.add_argument("--db", default="state/prozorro_seen.db", help="SQLite для дедуплікації")
    parser.add_argument("--csv", help="записати результат у CSV")
    parser.add_argument("--html", help="записати HTML-звіт")
    parser.add_argument("--telegram", action="store_true", help="надіслати в Telegram")
    parser.add_argument("--all", action="store_true", help="показати всі збіги, не лише нові")
    parser.add_argument("--demo", action="store_true", help="працювати на збереженому знімку API, без мережі")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_stdout()
    args = parse_args(argv)
    cfg = load_config(args.config)

    if args.keyword:
        cfg["keywords"] = args.keyword
    if args.cpv:
        cfg["cpv_prefixes"] = args.cpv
    if args.min_amount is not None:
        cfg["min_amount"] = args.min_amount
    if args.max_amount is not None:
        cfg["max_amount"] = args.max_amount
    if args.region:
        cfg["region"] = args.region
    if args.status:
        cfg["statuses"] = args.status
    if args.max_pages:
        cfg["max_pages"] = args.max_pages
    if args.inspect:
        cfg["max_inspect"] = args.inspect

    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    store = SeenStore(args.db)

    scanned = 0
    rows: list[dict] = []
    if args.demo:
        print("[i] Демо-режим: дані з fixtures/prozorro_tenders.json, мережа не використовується.")
        source = demo_tenders()
    else:
        source = iter_tenders(cfg)
    try:
        for tender in source:
            scanned += 1
            if not matches(tender, cfg):
                continue
            tender_id = tender.get("id") or ""
            if not args.all and not store.is_new(tender_id):
                continue
            rows.append(format_row(tender))
            store.remember(tender)
            if len(rows) >= 50:
                break
    finally:
        store.close()

    print(f"Переглянуто тендерів: {scanned}. Нових за фільтрами: {len(rows)}.")
    if scanned and cfg.get("max_inspect") and scanned >= int(cfg["max_inspect"]):
        print(f"[i] Досягнуто ліміт --inspect {cfg['max_inspect']}: за один прогін дивимось найновіші.")
    print_rows(rows)

    if args.csv and rows:
        write_csv(rows, args.csv)
        print(f"\nCSV: {args.csv}")
    if args.html:
        write_html(rows, args.html, cfg)
        print(f"HTML: {args.html}")
    if args.telegram and rows and not args.demo:
        send_telegram(rows)
    elif args.telegram and args.demo:
        print("[i] У демо-режимі Telegram не надсилається.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
