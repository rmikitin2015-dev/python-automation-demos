#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Пошук потенційних клієнтів серед переможців тендерів Prozorro.

Ідея: компанія, яка вже виграла публічну закупівлю, має бюджет і біль: щодня
переглядати сотні нових тендерів вручну. Це найкращий холодний контакт для
послуги «моніторинг тендерів».

Два джерела (--source):
  contracts — підписані договори, тобто переможці з реальними сумами
              (типово: найбільше контактів за один прогін);
  tenders   — самі тендери через awards (корисно для свіжих перемог).

Що робить:
  - читає відкритий API Prozorro (без ключа);
  - добирає об'єкти за останні N днів;
  - витягує переможців: назва, ЄДРПОУ, регіон, сума, ДК 021;
  - агрегує по компаніях: скільки перемог, на яку суму, коли остання;
  - пише prospects.csv + prospects.html, відсортовано за активністю.

Чесно про контакти: Prozorro НЕ публікує телефони/пошту постачальників.
Скрипт дає назву, ЄДРПОУ, регіон і посилання на тендер — контакт (сайт, телефон,
LinkedIn, сторінка в ЄДР) ти добираєш за 30 секунд. Це і коректніше юридично:
публічні службові контакти, а не зіскановані персональні дані.

Приклади:
  python prospect_finder.py --days 14 --limit 200
  python prospect_finder.py --days 30 --cpv 45 --limit 150
  python prospect_finder.py --days 30 --min-amount 500000 --region Київ
  python prospect_finder.py --source tenders --days 60 --cpv 72 --out out/prospects_it
"""

import argparse
import csv
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

API_ROOT = "https://public-api.prozorro.gov.ua/api/2.5"
SOURCES = {
    "contracts": {
        "url": f"{API_ROOT}/contracts",
        "fields": "title,status,value,suppliers,buyer,items,dateSigned,dateModified,contractID,tender_id",
    },
    "tenders": {
        "url": f"{API_ROOT}/tenders",
        "fields": "title,status,value,tenderID,items,procuringEntity,awards,dateModified",
    },
}


def setup_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def fetch_json(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": "prospect-finder/1.0 (freelance demo)"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_detail(source: str, obj_id: str) -> dict | None:
    conf = SOURCES[source]
    url = f"{conf['url']}/{obj_id}?{urllib.parse.urlencode({'opt_fields': conf['fields']})}"
    try:
        return (fetch_json(url) or {}).get("data")
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            return None  # закритий або видалений об'єкт
        print(f"[!] {obj_id}: HTTP {exc.code}", file=sys.stderr)
        return None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"[!] {obj_id}: {exc}", file=sys.stderr)
        return None


def as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def cpv_codes(tender: dict) -> list[str]:
    codes = []
    for item in tender.get("items") or []:
        code = ((item.get("classification") or {}).get("id") or "").strip()
        if code and code not in codes:
            codes.append(code)
    return codes


def regions(obj: dict) -> list[str]:
    found = []
    for item in obj.get("items") or []:
        region = ((item.get("deliveryAddress") or {}).get("region") or "").strip()
        if region and region not in found:
            found.append(region)
    suppliers = list(obj.get("suppliers") or [])
    for award in obj.get("awards") or []:
        suppliers.extend(award.get("suppliers") or [])
    for supplier in suppliers:
        region = ((supplier.get("address") or {}).get("region") or "").strip()
        if region and region not in found:
            found.append(region)
    return found


def parse_dt(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp
    except ValueError:
        return None


def iter_details(source: str, days: int, limit: int, delay: float):
    """Йде по фіду від новіших до старіших, доки не вийде за days або limit."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    feed_url = SOURCES[source]["url"]
    offset = None
    inspected = 0

    while inspected < limit:
        query = {"descending": "1", "limit": 100}
        if offset:
            query["offset"] = offset
        try:
            payload = fetch_json(f"{feed_url}?{urllib.parse.urlencode(query)}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"[!] Фід недоступний: {exc}", file=sys.stderr)
            return

        batch = payload.get("data") or []
        if not batch:
            return

        for ref in batch:
            obj = fetch_detail(source, ref.get("id") or "")
            inspected += 1
            if obj:
                yield obj
            if inspected >= limit:
                return
            if delay:
                time.sleep(delay)

        stamp = parse_dt(batch[-1].get("dateModified") or "")
        if stamp and stamp < cutoff:
            return

        next_page = payload.get("next_page") or {}
        offset = next_page.get("offset")
        if not offset:
            return


def deals_from(obj: dict, source: str) -> list[dict]:
    """Нормалізує контракт або тендер у ланки «переможець + сума»."""
    codes = cpv_codes(obj)
    found_regions = regions(obj)

    if source == "contracts":
        contract_id = obj.get("contractID") or ""
        # contractID має вигляд UA-2026-11-17-000035-a-a1; для публічного посилання
        # потрібен саме tenderID (без суфікса -a1/-b1), а не внутрішній id.
        tender_ref = re.sub(r"-[ab]\d+$", "", contract_id) or obj.get("tender_id") or ""
        base = {
            "amount": as_float((obj.get("value") or {}).get("amount")),
            "date": (obj.get("dateSigned") or obj.get("dateModified") or "")[:10],
            "ref_id": tender_ref or contract_id,
            "title": (obj.get("title") or "").strip(),
            "codes": codes,
            "regions": found_regions,
        }
        return [dict(base, supplier=s) for s in (obj.get("suppliers") or [])]

    deals = []
    tender_ref = obj.get("tenderID") or obj.get("id") or ""
    for award in obj.get("awards") or []:
        if award.get("status") not in ("active", "pending"):
            continue
        base = {
            "amount": as_float((award.get("value") or {}).get("amount"))
            or as_float((obj.get("value") or {}).get("amount")),
            "date": (award.get("date") or obj.get("dateModified") or "")[:10],
            "ref_id": tender_ref,
            "title": (obj.get("title") or "").strip(),
            "codes": codes,
            "regions": found_regions,
        }
        for supplier in award.get("suppliers") or []:
            deals.append(dict(base, supplier=supplier))
    return deals


def collect(source: str, days: int, limit: int, delay: float, cpv: list[str], region: str) -> dict:
    """Повертає агрегацію по переможцях: {edrpou: {...}}."""
    companies: dict = defaultdict(
        lambda: {
            "name": "",
            "edrpou": "",
            "wins": 0,
            "total": 0.0,
            "last_win": "",
            "regions": set(),
            "cpv": set(),
            "tenders": [],
        }
    )
    seen_deals = set()
    scanned = 0
    deals_count = 0

    for obj in iter_details(source, days, limit, delay):
        scanned += 1
        codes = cpv_codes(obj)
        if cpv and not any(code.startswith(tuple(cpv)) for code in codes):
            continue
        if region and region.lower() not in " ".join(regions(obj)).lower():
            continue

        for deal in deals_from(obj, source):
            supplier = deal["supplier"]
            name = (supplier.get("name") or "").strip()
            if not name:
                continue
            edrpou = ((supplier.get("identifier") or {}).get("id") or "").strip()
            key = edrpou or name.lower()
            dedupe = (key, deal["ref_id"], round(deal["amount"]))
            if dedupe in seen_deals:
                continue
            seen_deals.add(dedupe)

            record = companies[key]
            record["name"] = name
            record["edrpou"] = edrpou
            record["wins"] += 1
            record["total"] += deal["amount"]
            if deal["date"] > record["last_win"]:
                record["last_win"] = deal["date"]
            record["regions"].update(deal["regions"])
            record["cpv"].update(deal["codes"])
            if len(record["tenders"]) < 3 and deal["ref_id"]:
                record["tenders"].append(
                    {
                        "id": deal["ref_id"],
                        "title": deal["title"],
                        "url": f"https://prozorro.gov.ua/tender/{deal['ref_id']}",
                        "amount": deal["amount"],
                    }
                )
            deals_count += 1

    label = "договорів" if source == "contracts" else "тендерів"
    print(f"Переглянуто {label}: {scanned}. Знайдено ланок «об'єкт → переможець»: {deals_count}.")
    return companies


def to_rows(companies: dict, min_wins: int) -> list[dict]:
    rows = []
    for record in companies.values():
        if record["wins"] < min_wins:
            continue
        cpv_sorted = sorted(record["cpv"])[:3]
        rows.append(
            {
                "company": record["name"],
                "edrpou": record["edrpou"],
                "wins": record["wins"],
                "total_uah": round(record["total"]),
                "avg_uah": round(record["total"] / record["wins"]) if record["wins"] else 0,
                "last_win": record["last_win"],
                "regions": ", ".join(sorted(record["regions"])),
                "cpv": ", ".join(cpv_sorted),
                "tender_url": record["tenders"][0]["url"] if record["tenders"] else "",
                "tender_title": record["tenders"][0]["title"] if record["tenders"] else "",
                "contact": "",
                "status": "не писав",
            }
        )
    # Сортуємо за грошима, а не за кількістю: 7 перемог по 200 ₴ — це не клієнт.
    rows.sort(key=lambda r: (-r["total_uah"], -r["wins"]))
    return rows


def money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") + " ₴"


def write_csv(rows: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "company",
                "edrpou",
                "wins",
                "total_uah",
                "avg_uah",
                "last_win",
                "regions",
                "cpv",
                "contact",
                "status",
                "tender_title",
                "tender_url",
            ],
            delimiter=";",
        )
        writer.writeheader()
        writer.writerows(rows)


def write_html(rows: list[dict], path: str, filters: str, days: int) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    body = "\n".join(
        "<tr>"
        f"<td>{i}</td>"
        f"<td>{html.escape(r['company'])}</td>"
        f"<td>{html.escape(r['edrpou'])}</td>"
        f"<td class=\"num\">{r['wins']}</td>"
        f"<td class=\"num\">{money(r['total_uah'])}</td>"
        f"<td>{html.escape(r['last_win'])}</td>"
        f"<td>{html.escape(r['regions'])}</td>"
        f"<td>{html.escape(r['cpv'])}</td>"
        f"<td><a href=\"{html.escape(r['tender_url'])}\">тендер</a></td>"
        f"<td class=\"fill\"></td>"
        "</tr>"
        for i, r in enumerate(rows, 1)
    ) or "<tr><td colspan=\"10\">Нічого не знайдено — збільш --days або --limit</td></tr>"

    total = sum(r["total_uah"] for r in rows)
    doc = f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><title>Потенційні клієнти з Prozorro</title>
<style>
 body{{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:28px;color:#1b1b1b}}
 h1{{font-size:20px;margin:0 0 4px}} .meta{{color:#666;font-size:13px;margin-bottom:16px}}
 table{{border-collapse:collapse;width:100%;font-size:13px}}
 th,td{{border-bottom:1px solid #e6e6e6;padding:6px 8px;text-align:left;vertical-align:top}}
 th{{background:#f6f7f9;position:sticky;top:0}}
 td.num{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}}
 td.fill{{border-bottom:1px dashed #bbb;min-width:120px}}
 a{{color:#0a58ca;text-decoration:none}}
 .hint{{background:#fff8e1;border-left:3px solid #e0a800;padding:10px 14px;font-size:13px;margin:14px 0}}
</style></head><body>
<h1>Кому продавати моніторинг тендерів</h1>
<div class="meta">{datetime.now().strftime('%d.%m.%Y %H:%M')} · період: останні {days} дн. · фільтри: {html.escape(filters or 'без фільтрів')} · компаній: {len(rows)} · сумарно виграли: {money(total)}</div>
<div class="hint">Колонка праворуч — для контакту й позначки статусу («не писав» / «написав 12.11» / «відповів»). Контакти Prozorro не публікує: шукай сайт або сторінку в ЄДР за ЄДРПОУ.</div>
<table><thead><tr>
<th>#</th><th>Компанія</th><th>ЄДРПОУ</th><th>Перемог</th><th>Сума</th><th>Остання</th><th>Регіон</th><th>ДК 021</th><th>Тендер</th><th>Контакт / статус</th>
</tr></thead><tbody>
{body}
</tbody></table>
</body></html>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Переможці тендерів → список компаній для холодних контактів")
    parser.add_argument(
        "--source",
        choices=("contracts", "tenders"),
        default="contracts",
        help="contracts — підписані договори (типово), tenders — через awards",
    )
    parser.add_argument("--days", type=int, default=30, help="глибина у днях (типово 30)")
    parser.add_argument("--limit", type=int, default=150, help="скільки карток тендерів завантажити (типово 150)")
    parser.add_argument("--delay", type=float, default=0.12, help="пауза між запитами, сек")
    parser.add_argument("--cpv", action="append", default=[], help="префікс ДК 021, напр. 45 (можна кілька)")
    parser.add_argument("--region", help="частина назви області")
    parser.add_argument("--min-amount", type=float, default=0, help="мінімальна сума тендера, ₴")
    parser.add_argument("--min-wins", type=int, default=1, help="мінімум перемог у компанії (типово 1)")
    parser.add_argument("--top", type=int, default=50, help="скільки компаній залишити у списку (типово 50)")
    parser.add_argument("--out", default="out/prospects", help="префікс шляху для CSV/HTML")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_stdout()
    args = parse_args(argv)
    started = time.time()

    companies = collect(
        args.source, args.days, args.limit, args.delay, args.cpv, args.region or ""
    )
    rows = to_rows(companies, args.min_wins)
    rows = [r for r in rows if r["total_uah"] >= args.min_amount][: args.top]

    if not rows:
        print("Нічого не знайдено. Спробуй --days 60 --limit 250 або без --cpv.")
        return 1

    filters = ["джерело: " + ("договори" if args.source == "contracts" else "тендери")]
    if args.cpv:
        filters.append("ДК 021: " + ", ".join(args.cpv))
    if args.region:
        filters.append("регіон: " + args.region)
    if args.min_amount:
        filters.append(f"від {money(args.min_amount)}")

    csv_path, html_path = f"{args.out}.csv", f"{args.out}.html"
    write_csv(rows, csv_path)
    write_html(rows, html_path, "; ".join(filters), args.days)

    print(f"\nКомпаній у списку: {len(rows)} (за {time.time() - started:.0f} с)")
    print(f"{'#':>3} {'Компанія':<44} {'Перемог':>7} {'Сума':>14}  Регіон")
    for i, row in enumerate(rows[:12], 1):
        name = row["company"][:43]
        print(f"{i:>3} {name:<44} {row['wins']:>7} {money(row['total_uah']):>14}  {row['regions'][:28]}")

    print(f"\nCSV:  {csv_path}  (відкривається в Excel, колонки contact/status — для тебе)")
    print(f"HTML: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
