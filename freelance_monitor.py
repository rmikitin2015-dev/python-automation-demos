#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Монітор нових замовлень: freelance.ua (типово) і Freelancehunt.

Що робить:
  - читає публічний список замовлень freelance.ua або Freelancehunt
    (без ключів і реєстрації);
  - фільтрує локально — за ключовими словами, тегами, бюджетом і свіжістю;
  - запам'ятовує вже показані замовлення в SQLite, тому повторно їх не надсилає;
  - віддає результат у консоль, CSV, HTML і (опційно) у Telegram.

Залежності: тільки стандартна бібліотека Python 3.9+.

Приклади:
  python freelance_monitor.py
  python freelance_monitor.py -k python -k "телеграм бот" --min-budget 900 --pages 3
  python freelance_monitor.py --config freelance_config.example.json --csv out/orders.csv --html out/orders.html
  python freelance_monitor.py --telegram          # бере токен із TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
  python freelance_monitor.py --loop 600 --telegram   # перевіряти кожні 10 хвилин
  python freelance_monitor.py --config freelance_config.freelancehunt.json --loop 900 --telegram
                                                     # Freelancehunt, лише <10 ставок

Для розкладу (кожні 10 хв) на Windows:
  schtasks /create /tn "Freelance Monitor" /tr "python C:\path\freelance_monitor.py --telegram" /sc minute /mo 10
"""

import argparse
import csv
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_URL = "https://freelance.ua/orders/"
FH_BASE = "https://freelancehunt.com/ua/projects"

# Місяці в датах Freelancehunt: «21 листопада, 20:17».
FH_MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}

# Фільтри за замовчуванням — під мою спеціалізацію (Python: парсинг, автоматизація, боти).
DEFAULT_CONFIG = {
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
    "source": "freelance",       # "freelance" (freelance.ua) або "freelancehunt"
    "max_bids": None,             # 9 = лише замовлення з менш ніж 10 ставками
    "word_match": "substring",    # "prefix" — слово з початку, щоб «бот» не ловив «робота»
    "resolve_publish_time": False,  # Freelancehunt: брати точний час зі сторінки замовлення
}

# Одна розмітка: картка — це <li class="j-order ..."> всередині ul.l-projectList.
CARD_RE = re.compile(r'<li class="j-order[^"]*">(.*?)(?=<li class="j-order|</ul>)', re.S)
TITLE_RE = re.compile(r'<header class="l-project-title".*?<a href="([^"]+)"[^>]*>(.*?)</a>', re.S)
PRICE_RE = re.compile(r'<span class="l-price">\s*(.*?)\s*</span>', re.S)
TAG_RE = re.compile(r'<a[^>]*class="l-tag[^"]*"[^>]*>(.*?)</a>', re.S)
DESC_RE = re.compile(r"<article>\s*<p>(.*?)</p>", re.S)
AGE_RE = re.compile(r'fa fa-clock-o"></i>\s*(.*?)</li>', re.S)
PROPOSALS_RE = re.compile(r"(\d+)\s*пропозиц", re.S)
HOT_RE = re.compile(r'class="highlited-label"')
ID_RE = re.compile(r"/orders/(\d+)-")
TAG_TEXT_RE = re.compile(r"<[^>]+>")

# Розмітка Freelancehunt: картка — <article class="widget project-card ...">.
FH_CARD_SPLIT = '<article class="widget project-card'
FH_TITLE_RE = re.compile(r'<h2 class="list-card-title[^"]*"[^>]*>\s*<a href="([^"]+)"[^>]*>(.*?)</a>', re.S)
FH_STATS_RE = re.compile(r'<div class="project-stats">(.*?)</div>', re.S)
FH_BUDGET_RE = re.compile(r'class="project-stat budget">(.*?)</span>', re.S)
FH_BIDS_RE = re.compile(r"(\d+)\s*став[а-яіїєґ]*")  # «1 ставка», «52 ставки», «8 ставок»
FH_DATE_RE = re.compile(r'<span class="project-date">([^<]+)</span>')
FH_SKILL_RE = re.compile(r'class="project-skill">(.*?)</a>', re.S)
FH_DESC_RE = re.compile(r'<p class="project-description[^"]*"[^>]*>(.*?)</p>', re.S)
FH_PUBLISHED_RE = re.compile(r'Опубліковано:</span>\s*([^<]+)</li>', re.S)
FH_ID_RE = re.compile(r"/(\d+)\.html")


def setup_stdout() -> None:
    """Кирилиця у Windows-консолі та в пайпах без UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def load_config(path: str | None) -> dict:
    """Читає JSON-конфіг (або типові значення) і одразу нормалізує списки слів."""
    cfg = dict(DEFAULT_CONFIG)
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    return normalize_filters(cfg)


def normalize_filters(cfg: dict) -> dict:
    """Готує списки слів один раз: нижній регістр, без пробілів по краях і порожніх."""
    cfg = dict(cfg)
    for key in ("keywords", "excludes"):
        cfg[key] = [str(w).strip().lower() for w in (cfg.get(key) or []) if str(w).strip()]
    return cfg


def ensure_parent(path: str) -> None:
    """Створює батьківську теку для файлу, якщо її ще немає."""
    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)


def clean_text(raw: str) -> str:
    """Прибирає теги та HTML-сутності, згортає пробіли."""
    text = TAG_TEXT_RE.sub(" ", raw or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_budget(raw: str) -> float | None:
    """'900 грн.' -> 900.0, '1 200 грн' -> 1200.0, 'Договірна' -> None."""
    digits = re.sub(r"[^\d]", "", raw or "")
    return float(digits) if digits else None


def parse_age_hours(label: str) -> float | None:
    """'щойно', '15 хвилин тому', 'вчора', '3 днів тому' -> години."""
    text = clean_text(label).lower()
    if not text:
        return None
    if "щойно" in text or "секунд" in text:
        return 0.0
    if "позавчора" in text:
        return 48.0
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
    if "сьогодні" in text:
        return 1.0
    return None


def parse_orders(page_html: str) -> list[dict]:
    """Розбирає HTML списку замовлень у список словників."""
    orders: list[dict] = []
    for card_match in CARD_RE.finditer(page_html or ""):
        card = card_match.group(0)
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
                "hot": bool(HOT_RE.search(card)),
            }
        )
    return orders


def build_url(page: int = 1, source: str = "freelance") -> str:
    """URL списку замовлень: сторінка 1 — без параметрів, далі ?page=N."""
    base = FH_BASE if source == "freelancehunt" else BASE_URL
    return base if page <= 1 else base + "?" + urllib.parse.urlencode({"page": str(page)})


def parse_fh_moment(label: str, now: datetime | None = None) -> tuple[datetime | None, bool]:
    """«1:33» або «21 листопада, 20:17» -> (дата-час, чи відомий час доби)."""
    now = now or datetime.now()
    text = clean_text(label).lower()
    if not text:
        return None, False

    today = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if today:
        moment = now.replace(
            hour=int(today.group(1)), minute=int(today.group(2)), second=0, microsecond=0
        )
        return moment, True

    dotted = re.search(r"(\d{1,2})\.(\d{2})\.(\d{4})(?:,?\s*(\d{1,2}):(\d{2}))?", text)
    if dotted:
        try:
            moment = datetime(
                int(dotted.group(3)), int(dotted.group(2)), int(dotted.group(1)),
                int(dotted.group(4) or 0), int(dotted.group(5) or 0),
            )
        except ValueError:
            return None, False
        return moment, dotted.group(4) is not None

    words = re.search(
        r"(\d{1,2})\s+([а-яіїєґ]+)(?:\s+(\d{4}))?(?:,?\s*(\d{1,2}):(\d{2}))?", text
    )
    if words and words.group(2) in FH_MONTHS:
        year = int(words.group(3)) if words.group(3) else now.year
        try:
            moment = datetime(
                year, FH_MONTHS[words.group(2)], int(words.group(1)),
                int(words.group(4) or 0), int(words.group(5) or 0),
            )
        except ValueError:
            return None, False
        if not words.group(3) and moment > now:
            moment = moment.replace(year=year - 1)
        return moment, words.group(4) is not None

    return None, False


def parse_fh_orders(page_html: str, now: datetime | None = None) -> list[dict]:
    """Розбирає стрічку Freelancehunt (картки .project-card) у список словників."""
    now = now or datetime.now()
    orders: list[dict] = []
    for chunk in (page_html or "").split(FH_CARD_SPLIT)[1:]:
        title_match = FH_TITLE_RE.search(chunk)
        if not title_match:
            continue
        url = html.unescape(title_match.group(1)).strip()
        id_match = FH_ID_RE.search(url)
        if not id_match:
            continue

        stats_match = FH_STATS_RE.search(chunk)
        stats_html = stats_match.group(1) if stats_match else ""
        budget_match = FH_BUDGET_RE.search(stats_html)
        budget_raw = clean_text(budget_match.group(1)) if budget_match else ""
        bids_match = FH_BIDS_RE.search(clean_text(stats_html))
        date_match = FH_DATE_RE.search(chunk)
        label = clean_text(date_match.group(1)) if date_match else ""
        published, time_known = parse_fh_moment(label, now)
        tags = [clean_text(tag) for tag in FH_SKILL_RE.findall(chunk)]
        desc_match = FH_DESC_RE.search(chunk)

        orders.append(
            {
                "id": id_match.group(1),
                "title": clean_text(title_match.group(2)),
                "url": url,
                "site": "Freelancehunt",
                "budget_raw": budget_raw,
                "budget": parse_budget(budget_raw),
                "description": clean_text(desc_match.group(1)) if desc_match else "",
                "age_label": label,
                "age_hours": (
                    round(max(0.0, (now - published).total_seconds() / 3600.0), 2)
                    if published and time_known
                    else None
                ),
                "published_at": published.strftime("%Y-%m-%d %H:%M") if published else "",
                "proposals": int(bids_match.group(1)) if bids_match else None,
                "tags": [tag for tag in tags if tag],
                "hot": "Терміновий" in chunk,
            }
        )
    return orders


BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
}


def fetch_page(url: str, user_agent: str, timeout: int = 30) -> str:
    """Читає сторінку; на 403/429 повторює через системний curl."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, **BROWSER_HEADERS})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code not in (403, 429):
            raise
        return fetch_with_curl(url, user_agent, timeout)


def fetch_with_curl(url: str, user_agent: str, timeout: int = 30) -> str:
    """Freelancehunt відхиляє urllib (403), тому другий шанс — curl із браузерним UA."""
    curl = shutil.which("curl")
    if not curl:
        raise OSError("curl не знайдено, а сайт відповів 403")
    proc = subprocess.run(
        [
            curl, "-sSL", "--max-time", str(timeout),
            "-A", user_agent, "-H", "Accept-Language: uk-UA,uk;q=0.9", url,
        ],
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise OSError(detail or f"curl завершився з кодом {proc.returncode}")
    return proc.stdout.decode("utf-8", errors="replace")


FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def demo_orders() -> list[dict]:
    """Замовлення зі збереженої сторінки — той самий розбір, але без мережі."""
    with open(os.path.join(FIXTURES_DIR, "freelance_orders.html"), "r", encoding="utf-8") as fh:
        return parse_orders(fh.read())


def collect(cfg: dict) -> list[dict]:
    """Обходить сторінки списку й збирає замовлення без дублів."""
    delay = float(cfg.get("request_delay") or 0)
    pages = max(1, int(cfg.get("pages") or 1))
    ua = cfg.get("user_agent") or DEFAULT_CONFIG["user_agent"]
    source = str(cfg.get("source") or "freelance")
    parser = parse_fh_orders if source == "freelancehunt" else parse_orders

    collected: dict[str, dict] = {}
    for page in range(1, pages + 1):
        url = build_url(page, source)
        try:
            page_html = fetch_page(url, ua)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"[!] Не вдалося прочитати {url}: {exc}", file=sys.stderr)
            continue

        for order in parser(page_html):
            collected.setdefault(order["id"], order)

        if delay:
            time.sleep(delay)

    orders = list(collected.values())
    if source == "freelancehunt" and cfg.get("resolve_publish_time"):
        orders = resolve_fh_publish_time(orders, cfg, ua)
    return orders


def resolve_fh_publish_time(orders: list[dict], cfg: dict, ua: str) -> list[dict]:
    """У стрічці час є лише для сьогоднішніх — для кандидатів добираємо зі сторінки."""
    delay = float(cfg.get("request_delay") or 0)
    for order in orders:
        if order.get("age_hours") is not None:
            continue
        if not (_budget_ok(order, cfg) and _bids_ok(order, cfg) and _text_ok(order, cfg)):
            continue
        try:
            page_html = fetch_page(order["url"], ua)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"[!] Не вдалося прочитати {order['url']}: {exc}", file=sys.stderr)
            continue
        match = FH_PUBLISHED_RE.search(page_html)
        if match:
            published, known = parse_fh_moment(match.group(1))
            if published and known:
                order["published_at"] = published.strftime("%Y-%m-%d %H:%M")
                age = (datetime.now() - published).total_seconds() / 3600.0
                order["age_hours"] = round(max(0.0, age), 2)
        if delay:
            time.sleep(delay)
    return orders


def _budget_ok(order: dict, cfg: dict) -> bool:
    """Бюджет: «Договірна» (None) не відсікається — ціна невідома, а не низька."""
    budget = order.get("budget")
    min_budget = cfg.get("min_budget")
    max_budget = cfg.get("max_budget")
    if budget is not None:
        if min_budget and budget < float(min_budget):
            return False
        if max_budget and budget > float(max_budget):
            return False
    return True


def _bids_ok(order: dict, cfg: dict) -> bool:
    """Кількість ставок: max_bids=9 пропускає все, де ставок менше 10.

    Якщо кількість невідома (None), замовлення відкидається: умову
    «менше 10 ставок» тоді підтвердити неможливо.
    """
    limit = cfg.get("max_bids")
    if limit is None:
        return True
    bids = order.get("proposals")
    return bids is not None and int(bids) <= int(limit)


def _hit(haystack: str, words: list[str], mode: str) -> bool:
    """Пошук слів: substring (типово) або prefix — слово з початку слова."""
    for word in words:
        if mode == "prefix":
            if re.search(r"(?<![0-9a-zа-яіїєґ])" + re.escape(word), haystack):
                return True
        elif word in haystack:
            return True
    return False


def _text_ok(order: dict, cfg: dict) -> bool:
    haystack = " ".join(
        [
            order.get("title") or "",
            order.get("description") or "",
            " ".join(order.get("tags") or []),
        ]
    ).lower()
    mode = str(cfg.get("word_match") or "substring")

    if _hit(haystack, cfg.get("excludes") or [], mode):
        return False

    keywords = cfg.get("keywords") or []
    return not keywords or _hit(haystack, keywords, mode)


def _age_ok(order: dict, cfg: dict) -> bool:
    max_age = cfg.get("max_age_hours")
    age = order.get("age_hours")
    if max_age and age is not None and age > float(max_age):
        return False
    return True


def matches(order: dict, cfg: dict) -> bool:
    """Локальний фільтр: бюджет, ставки, свіжість, слова.

    cfg має бути з load_config() або normalize_filters(): там списки слів уже
    приведені до нижнього регістру й обрізані, тому тут їх не перераховуємо.
    """
    return (
        _budget_ok(order, cfg)
        and _bids_ok(order, cfg)
        and _age_ok(order, cfg)
        and _text_ok(order, cfg)
    )


class SeenStore:
    """Дедуплікація: кожне замовлення показуємо один раз."""

    def __init__(self, path: str):
        if path != ":memory:":
            ensure_parent(path)
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
        "site": order.get("site") or "freelance.ua",
        "title": (order.get("title") or "").strip(),
        "budget": f"{budget:,.0f}".replace(",", " ") if budget is not None else (order.get("budget_raw") or ""),
        "tags": ", ".join(order.get("tags") or []),
        "published": order.get("age_label") or order.get("published_at") or "",
        "proposals": "" if order.get("proposals") is None else str(order.get("proposals")),
        "hot": "гаряче" if order.get("hot") else "",
        "description": (order.get("description") or "")[:200],
        "url": order.get("url") or "",
    }


def print_rows(rows: list[dict]) -> None:
    for row in rows:
        print(f"\n[{row['id']}] {row['title']}")
        hot = " | 🔥 гаряче" if row.get("hot") else ""
        print(f"  Бюджет: {row['budget'] or '—'} | Опубліковано: {row['published'] or '—'} | Ставок: {row['proposals'] or '—'}{hot}")
        if row["tags"]:
            print(f"  Теги: {row['tags']}")
        if row["description"]:
            print(f"  {row['description']}")
        print(f"  {row['url']}")


def write_csv(rows: list[dict], path: str) -> None:
    ensure_parent(path)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def write_html(rows: list[dict], path: str, cfg: dict) -> None:
    ensure_parent(path)
    site = "Freelancehunt" if str(cfg.get("source") or "") == "freelancehunt" else "freelance.ua"
    filters = []
    if cfg.get("keywords"):
        filters.append("ключові слова: " + ", ".join(str(k) for k in cfg["keywords"]))
    if cfg.get("max_bids") is not None:
        filters.append(f"ставок не більше {cfg['max_bids']}")
    if cfg.get("min_budget"):
        filters.append(f"бюджет від {cfg['min_budget']} грн")

    lines = []
    for r in rows:
        badge = ' <span class="hot">гаряче</span>' if r.get("hot") else ""
        lines.append(
            "<tr>"
            f'<td class="num">{html.escape(r["budget"] or "—")}</td>'
            f'<td><a href="{html.escape(r["url"])}">{html.escape(r["title"])}</a>{badge}'
            f'<div class="tags">{html.escape(r["tags"] or "")}</div></td>'
            f'<td>{html.escape(r["published"] or "—")}</td>'
            f'<td class="num">{html.escape(r["proposals"])}</td>'
            "</tr>"
        )
    body = "\n".join(lines) or "<tr><td colspan=\"4\">Нових замовлень за фільтрами немає.</td></tr>"

    doc = f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8">
<title>Нові замовлення {html.escape(site)}</title>
<style>
 body{{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:32px;color:#1b1b1b}}
 h1{{font-size:20px;margin:0 0 4px}}
 .meta{{color:#666;font-size:13px;margin-bottom:18px}}
 table{{border-collapse:collapse;width:100%;font-size:14px}}
 th,td{{border-bottom:1px solid #e3e3e3;padding:8px 10px;text-align:left;vertical-align:top}}
 th{{background:#f6f7f9;font-weight:600}}
 td.num{{text-align:right;white-space:nowrap}}
 .tags{{color:#777;font-size:12px;margin-top:3px}}
 .hot{{background:#fff1e6;color:#a3540a;border-radius:4px;padding:0 4px;font-size:11px}}
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
    site = row.get("site") or "freelance.ua"
    parts = [f"Нове замовлення на {site}", f"{row['title']}"]
    if row.get("hot"):
        parts.append("🔥 гаряче")
    parts.append(f"Бюджет: {row['budget'] or '—'} · ставок: {row['proposals'] or '—'}")
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
    if getattr(args, "demo", False):
        print("[i] Демо-режим: дані з fixtures/freelance_orders.html, мережа не використовується.")
        source = demo_orders()
    else:
        source = collect(cfg)
    try:
        for order in source:
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
    if args.telegram and rows and not getattr(args, "demo", False):
        send_telegram(rows)
    elif args.telegram and getattr(args, "demo", False):
        print("[i] У демо-режимі Telegram не надсилається.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Монітор нових замовлень freelance.ua")
    parser.add_argument("--config", help="JSON-файл з фільтрами")
    parser.add_argument("-k", "--keyword", action="append", default=[], help="ключове слово для фільтра (можна кілька)")
    parser.add_argument("--exclude", action="append", default=[], help="слово-виключення (можна кілька)")
    parser.add_argument("--min-budget", type=float, default=None, help="мінімальний бюджет, грн")
    parser.add_argument("--max-budget", type=float, default=None, help="максимальний бюджет, грн")
    parser.add_argument("--max-age", type=float, default=None, help="максимальний вік замовлення, годин")
    parser.add_argument("--pages", type=int, default=None, help="скільки сторінок списку читати")
    parser.add_argument("--source", choices=["freelance", "freelancehunt"], default=None,
                        help="джерело: freelance.ua (типово) або Freelancehunt")
    parser.add_argument("--max-bids", type=int, default=None,
                        help="максимум ставок у замовленні (9 = менше 10)")
    parser.add_argument("--db", default="state/freelance_seen.db", help="SQLite для дедуплікації")
    parser.add_argument("--csv", help="записати результат у CSV")
    parser.add_argument("--html", help="записати HTML-звіт")
    parser.add_argument("--telegram", action="store_true", help="надіслати в Telegram")
    parser.add_argument("--all", action="store_true", help="показати всі збіги, не лише нові")
    parser.add_argument("--demo", action="store_true", help="працювати на збереженій HTML-фікстурі, без мережі")
    parser.add_argument("--loop", type=int, default=0, help="перевіряти кожні N секунд (0 — один прогін)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_stdout()
    args = parse_args(argv)
    cfg = load_config(args.config)

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
    if args.pages is not None:
        cfg["pages"] = args.pages
    if args.source:
        cfg["source"] = args.source
    if args.max_bids is not None:
        cfg["max_bids"] = args.max_bids
    if args.source == "freelancehunt":
        cfg["resolve_publish_time"] = True

    # CLI-прапорці перекривають конфіг, тому після них слова треба нормалізувати ще раз.
    cfg = normalize_filters(cfg)

    if args.loop > 0:
        while True:
            run_once(args, cfg)
            print(f"\n[i] Наступна перевірка через {args.loop} с. Ctrl+C — вихід.")
            time.sleep(args.loop)
    return run_once(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
