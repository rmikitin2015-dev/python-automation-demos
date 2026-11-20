#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Тести демо-скриптів. Мережа не використовується — тільки чиста логіка.

Запуск:
    python -m unittest discover -s tests -v
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bank_report  # noqa: E402
import booking_bot  # noqa: E402
import freelance_monitor  # noqa: E402
import prozorro_monitor  # noqa: E402
import prospect_finder  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_CSV = os.path.join(os.path.dirname(HERE), "sample_statement.csv")


class TestBankReport(unittest.TestCase):
    def test_parse_amount_formats(self):
        cases = {
            "-842,50": -842.5,
            "1 234,56": 1234.56,
            "₴ 1 000": 1000.0,
            "1,234.56": 1234.56,
            "12": 12.0,
            "": None,
            "не число": None,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(bank_report.parse_amount(raw), expected)

    def test_categorize(self):
        rules = bank_report.DEFAULT_RULES
        self.assertEqual(bank_report.categorize("Сільпо, Київ", -842.5, rules), "Продукти")
        self.assertEqual(bank_report.categorize("Uber Trip Kyiv", -148.0, rules), "Транспорт")
        self.assertEqual(bank_report.categorize("Зарплата ТОВ Ромашка", 42000.0, rules), "Надходження")
        self.assertEqual(bank_report.categorize("Щось невідоме", -10.0, rules), "Інше")

    def test_normalize_sample_statement(self):
        text = bank_report.read_text(SAMPLE_CSV)
        records = bank_report.normalize(bank_report.sniff_rows(text), bank_report.DEFAULT_RULES)

        self.assertEqual(len(records), 31)
        income = sum(r["amount"] for r in records if r["amount"] > 0)
        expenses = -sum(r["amount"] for r in records if r["amount"] < 0)
        self.assertAlmostEqual(income, 52199.0, places=2)
        self.assertAlmostEqual(expenses, 37418.64, places=2)
        self.assertTrue(all(r["date"].startswith("2026-") for r in records))

    def test_parse_date_variants(self):
        self.assertEqual(bank_report.parse_date("01.10.2026"), "2026-10-01")
        self.assertEqual(bank_report.parse_date("2026-10-01"), "2026-10-01")


class TestBookingBot(unittest.TestCase):
    def setUp(self):
        self.storage = booking_bot.Storage(":memory:")
        self.bot = booking_bot.BookingBot(self.storage)

    def tearDown(self):
        self.storage.close()

    def test_welcome_lists_services(self):
        out = self.bot.handle("u1", text="/start Олена")[0]
        self.assertIn("Вітаю, Олена", out.text)
        codes = [code for row in out.buttons for _, code in row]
        self.assertIn("svc|1", codes)

    def test_full_booking_flow_and_no_double_booking(self):
        days = booking_bot.free_days(self.storage, booking_bot.CONFIG)
        self.assertTrue(days, "має бути хоча б один вільний день")
        day = days[0]

        hours = booking_bot.free_hours(self.storage, booking_bot.CONFIG, day)
        self.assertTrue(hours)
        slot = booking_bot.slot_iso(day, hours[0])

        confirm = self.bot.handle("u1", callback=f"confirm|{slot}|1")[0]
        self.assertIn("Готово", confirm.text)
        self.assertEqual(len(self.storage.by_user("u1")), 1)

        # Другий клієнт на той самий час не проходить
        second = self.bot.handle("u2", callback=f"confirm|{slot}|1")[0]
        self.assertIn("зайняли", second.text)
        self.assertEqual(len(self.storage.by_user("u2")), 0)

    def test_cancel(self):
        day = booking_bot.free_days(self.storage, booking_bot.CONFIG)[0]
        slot = booking_bot.slot_iso(day, booking_bot.free_hours(self.storage, booking_bot.CONFIG, day)[0])
        self.bot.handle("u3", callback=f"confirm|{slot}|1")

        cancel = self.bot.handle("u3", text="/cancel 1")[0]
        self.assertIn("скасовано", cancel.text)
        self.assertEqual(self.storage.by_user("u3"), [])

    def test_cannot_cancel_foreign_appointment(self):
        day = booking_bot.free_days(self.storage, booking_bot.CONFIG)[0]
        slot = booking_bot.slot_iso(day, booking_bot.free_hours(self.storage, booking_bot.CONFIG, day)[0])
        self.bot.handle("owner", callback=f"confirm|{slot}|1")

        alien = self.bot.handle("stranger", text="/cancel 1")[0]
        self.assertIn("Не знайшов", alien.text)


class TestProzorroMonitor(unittest.TestCase):
    def tender(self, **overrides):
        base = {
            "id": "abc",
            "tenderID": "UA-2026-11-17-000027-a",
            "title": "Капітальний ремонт вхідної групи",
            "status": "active.tendering",
            "value": {"amount": 192308.0},
            "items": [
                {
                    "classification": {"id": "45420000-7"},
                    "deliveryAddress": {"region": "Житомирська область"},
                }
            ],
            "procuringEntity": {"name": "Поліський національний університет"},
            "tenderPeriod": {"endDate": "2026-12-03T00:00:00+02:00"},
        }
        base.update(overrides)
        return base

    def test_filters_pass(self):
        cfg = dict(prozorro_monitor.DEFAULT_CONFIG)
        cfg.update({"keywords": ["ремонт"], "cpv_prefixes": ["45"], "min_amount": 100000, "region": "Житомир"})
        self.assertTrue(prozorro_monitor.matches(self.tender(), cfg))

    def test_filters_reject(self):
        cfg = dict(prozorro_monitor.DEFAULT_CONFIG)
        cfg["keywords"] = ["вентиляція"]
        self.assertFalse(prozorro_monitor.matches(self.tender(), cfg))

        cfg = dict(prozorro_monitor.DEFAULT_CONFIG)
        cfg["cpv_prefixes"] = ["72"]
        self.assertFalse(prozorro_monitor.matches(self.tender(), cfg))

        cfg = dict(prozorro_monitor.DEFAULT_CONFIG)
        cfg["min_amount"] = 500000
        self.assertFalse(prozorro_monitor.matches(self.tender(), cfg))

        cfg = dict(prozorro_monitor.DEFAULT_CONFIG)
        cfg["region"] = "Львів"
        self.assertFalse(prozorro_monitor.matches(self.tender(), cfg))

    def test_formatting(self):
        self.assertEqual(prozorro_monitor.tender_deadline(self.tender()), "2026-12-03 00:00")
        row = prozorro_monitor.format_row(self.tender())
        self.assertEqual(row["cpv"], "45420000-7")
        self.assertIn("192 308", row["amount"])
        self.assertTrue(row["url"].endswith("UA-2026-11-17-000027-a"))


ORDER_LIST_HTML = """
<div class="j-list"><ul class="l-projectList"><li class="j-order  highlited">
  <span class="highlited-label">гаряче</span>
  <header class="l-project-title"><a href="https://freelance.ua/orders/298308-parser-czin-na-avito.html">Парсер цін на Авіто, Python</a></header>
  <div class="l-project-head flex-price-tag"><span class="l-price">900 грн. </span>
    <a href="https://freelance.ua/?tag=python-1" class="l-tag">python</a>
    <a href="https://freelance.ua/?tag=parsing-2" class="l-tag">парсинг</a>
  </div>
  <article><p>Потрібен скрипт на Python, який збирає ціни та віддає їх у Google Sheets.</p></article>
  <ul class="l-item-features"><li><i class="fa fa-clock-o"></i> 2 години тому</li>
    <li><span>4 пропозицій</span></li></ul>
</li><li class="j-order">
  <header class="l-project-title"><a href="https://freelance.ua/orders/298309-logo-dlya-kawi.html">Логотип для кав'ярні</a></header>
  <div class="l-project-head flex-price-tag"><span class="l-price">1 200 грн. </span>
    <a href="https://freelance.ua/?tag=design-3" class="l-tag">дизайн</a>
  </div>
  <article><p>Шукаю дизайнера з портфоліо.</p></article>
  <ul class="l-item-features"><li><i class="fa fa-clock-o"></i> вчора</li>
    <li><span>11 пропозицій</span></li></ul>
</li></ul></div>
"""


class TestFreelanceMonitor(unittest.TestCase):
    def test_parse_orders_from_html(self):
        orders = freelance_monitor.parse_orders(ORDER_LIST_HTML)
        self.assertEqual(len(orders), 2)

        first = orders[0]
        self.assertEqual(first["id"], "298308")
        self.assertEqual(first["title"], "Парсер цін на Авіто, Python")
        self.assertEqual(first["budget"], 900.0)
        self.assertEqual(first["tags"], ["python", "парсинг"])
        self.assertEqual(first["proposals"], 4)
        self.assertEqual(first["age_hours"], 2.0)
        self.assertTrue(first["hot"])
        self.assertIn("Google Sheets", first["description"])

        self.assertEqual(orders[1]["budget"], 1200.0)
        self.assertEqual(orders[1]["age_hours"], 24.0)
        self.assertFalse(orders[1]["hot"])

    def test_parse_budget_formats(self):
        cases = {"900 грн.": 900.0, "1 200 грн": 1200.0, "80000 грн. ": 80000.0, "Договірна": None, "": None}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(freelance_monitor.parse_budget(raw), expected)

    def test_parse_age_hours(self):
        cases = {
            "щойно": 0.0,
            "30 хвилин тому": 0.5,
            "3 години тому": 3.0,
            "вчора": 24.0,
            "позавчора": 48.0,
            "3 днів тому": 72.0,
            "2 тижні тому": 336.0,
            "": None,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(freelance_monitor.parse_age_hours(raw), expected)

    def test_build_url(self):
        self.assertEqual(freelance_monitor.build_url(1), "https://freelance.ua/orders/")
        self.assertEqual(freelance_monitor.build_url(2), "https://freelance.ua/orders/?page=2")
        self.assertNotIn("page", freelance_monitor.build_url(1))
        self.assertNotIn("q=", freelance_monitor.build_url(5))  # пошуку на сайті більше немає

    def test_filters(self):
        cfg = dict(freelance_monitor.DEFAULT_CONFIG)
        cfg.update({"keywords": ["python", "парс"], "min_budget": 500, "max_age_hours": 72})
        orders = freelance_monitor.parse_orders(ORDER_LIST_HTML)
        self.assertTrue(freelance_monitor.matches(orders[0], cfg))
        self.assertFalse(freelance_monitor.matches(orders[1], cfg))  # немає ключових слів

        cfg_low = dict(cfg, min_budget=1000)
        self.assertFalse(freelance_monitor.matches(orders[0], cfg_low))  # 900 < 1000

        cfg_old = dict(cfg, max_age_hours=24)
        old_order = dict(orders[0], age_hours=48.0)
        self.assertFalse(freelance_monitor.matches(old_order, cfg_old))

        cfg_exclude = dict(cfg, excludes=["sheets"])
        self.assertFalse(freelance_monitor.matches(orders[0], cfg_exclude))

    def test_unknown_budget_passes_budget_filter(self):
        # «Договірна»: ціна невідома, тому min_budget/max_budget її не відсікають
        cfg = freelance_monitor.normalize_filters(
            dict(freelance_monitor.DEFAULT_CONFIG, keywords=["python"], min_budget=5000, max_budget=6000)
        )
        order = dict(freelance_monitor.parse_orders(ORDER_LIST_HTML)[0], budget=None, budget_raw="Договірна")
        self.assertTrue(freelance_monitor.matches(order, cfg))

        # а от відома ціна нижче мінімуму — відсікається
        cheap = dict(order, budget=900.0, budget_raw="900 грн.")
        self.assertFalse(freelance_monitor.matches(cheap, cfg))

    def test_normalize_filters(self):
        cfg = freelance_monitor.normalize_filters(
            {"keywords": [" Python ", "ПАРС", ""], "excludes": [" 1С "]}
        )
        self.assertEqual(cfg["keywords"], ["python", "парс"])
        self.assertEqual(cfg["excludes"], ["1с"])

    def test_load_config_normalizes_filters(self):
        # контракт: конфіг приходить зі load_config уже нормалізованим
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cfg.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"keywords": [" Python ", "ПАРС", ""], "excludes": [" 1С "]}, fh)
            cfg = freelance_monitor.load_config(path)

        self.assertEqual(cfg["keywords"], ["python", "парс"])
        self.assertEqual(cfg["excludes"], ["1с"])
        order = freelance_monitor.parse_orders(ORDER_LIST_HTML)[0]
        self.assertTrue(freelance_monitor.matches(order, cfg))

    def test_seen_store_dedup(self):
        store = freelance_monitor.SeenStore(":memory:")
        self.addCleanup(store.close)
        order = freelance_monitor.parse_orders(ORDER_LIST_HTML)[0]

        self.assertTrue(store.is_new(order["id"]))
        store.remember(order)
        self.assertFalse(store.is_new(order["id"]))

    def test_telegram_text(self):
        row = freelance_monitor.format_row(freelance_monitor.parse_orders(ORDER_LIST_HTML)[0])
        text = freelance_monitor.telegram_text(row)
        self.assertIn("298308-parser-czin-na-avito", text)
        self.assertIn("900", text)
        self.assertIn("гаряче", text)  # перша картка фікстури має мітку «гаряче»
        self.assertIn("freelance.ua/orders/", text)


class TestProspectFinder(unittest.TestCase):
    def test_contract_deal_uses_public_tender_id(self):
        contract = {
            "contractID": "UA-2026-11-17-000035-a-a1",
            "tender_id": "5e68c51364b84d5d9f499e4831aca660",
            "title": "Інвертори",
            "value": {"amount": 1762500.0},
            "dateSigned": "2026-11-16T10:00:00+02:00",
            "suppliers": [{"name": "ТОВ ХКД-ТРЕЙД", "identifier": {"id": "34859931"}}],
            "items": [{"classification": {"id": "31150000-2"}}],
        }
        deals = prospect_finder.deals_from(contract, "contracts")

        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0]["ref_id"], "UA-2026-11-17-000035-a")
        self.assertEqual(deals[0]["amount"], 1762500.0)
        self.assertEqual(deals[0]["date"], "2026-11-16")

    def test_rows_sorted_by_money(self):
        companies = {
            "a": {
                "name": "Мала",
                "edrpou": "111",
                "wins": 7,
                "total": 3000.0,
                "last_win": "2026-11-10",
                "regions": {"Київ"},
                "cpv": {"45000000-7"},
                "tenders": [],
            },
            "b": {
                "name": "Велика",
                "edrpou": "222",
                "wins": 1,
                "total": 500000.0,
                "last_win": "2026-11-16",
                "regions": {"Львівська область"},
                "cpv": {"45220000-5"},
                "tenders": [],
            },
        }
        rows = prospect_finder.to_rows(companies, min_wins=1)
        self.assertEqual([r["company"] for r in rows], ["Велика", "Мала"])
        self.assertEqual(rows[0]["total_uah"], 500000)
        self.assertEqual(rows[1]["avg_uah"], round(3000 / 7))


class TestOfflineDemos(unittest.TestCase):
    """--demo читає збережені фікстури; будь-яке звернення в мережу — провал тесту."""

    def _offline(self):
        return mock.patch(
            "urllib.request.urlopen", side_effect=AssertionError("демо не має ходити в мережу")
        )

    def _run_quiet(self, func, argv):
        out = io.StringIO()
        with self._offline(), contextlib.redirect_stdout(out):
            code = func(argv)
        return code, out.getvalue()

    def test_freelance_demo_parses_fixture(self):
        with self._offline():
            orders = freelance_monitor.demo_orders()
        self.assertGreaterEqual(len(orders), 5)
        self.assertTrue(all(o["id"] and o["url"].startswith("https://") for o in orders))

    def test_each_demo_runs_offline_and_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases = [
                (
                    freelance_monitor.main,
                    ["--demo", "--all", "--db", os.path.join(tmp, "f.db"),
                     "--csv", os.path.join(tmp, "f.csv"), "--html", os.path.join(tmp, "f.html")],
                    ("f.csv", "f.html"),
                ),
                (
                    prozorro_monitor.main,
                    ["--demo", "--all", "--db", os.path.join(tmp, "p.db"),
                     "--html", os.path.join(tmp, "p.html")],
                    ("p.html",),
                ),
                (
                    prospect_finder.main,
                    ["--demo", "--out", os.path.join(tmp, "prospects")],
                    ("prospects.csv", "prospects.html"),
                ),
            ]
            for func, argv, expected in cases:
                with self.subTest(script=func.__module__):
                    code, printed = self._run_quiet(func, argv)
                    self.assertEqual(code, 0, printed)
                    self.assertIn("Демо-режим", printed)
                    self.assertIn("Переглянуто", printed)
                    for name in expected:
                        self.assertTrue(os.path.exists(os.path.join(tmp, name)), name)

    def test_demo_fixtures_carry_real_data(self):
        with self._offline():
            tenders = prozorro_monitor.demo_tenders()
            contracts = prospect_finder.demo_objects("contracts")
        self.assertGreaterEqual(len(tenders), 3)
        self.assertTrue(all(t["tenderID"] for t in tenders))
        with self._offline(), contextlib.redirect_stdout(io.StringIO()):
            companies = prospect_finder.collect(
                "contracts", 30, 0, 0, [], "", objects=contracts
            )
        self.assertGreaterEqual(len(companies), 3)


class TestSmoke(unittest.TestCase):
    def test_modules_importable_without_side_effects(self):
        for module in (bank_report, booking_bot, freelance_monitor, prozorro_monitor, prospect_finder):
            self.assertTrue(callable(module.main), module.__name__)

    def test_unit_helpers(self):
        label = booking_bot.human_slot("2026-11-17T10:00")
        self.assertTrue(label.startswith("вт 17.11"), label)
        self.assertTrue(label.endswith("10:00"), label)
        self.assertEqual(booking_bot.kind_of(9), "ранок")
        self.assertEqual(booking_bot.kind_of(20), "вечір")
        self.assertIsInstance(booking_bot.free_days(booking_bot.Storage(":memory:"), booking_bot.CONFIG), list)
        self.assertGreaterEqual(date.today().year, 2026)


if __name__ == "__main__":
    unittest.main(verbosity=2)
