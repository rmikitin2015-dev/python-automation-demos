#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram-бот запису на послуги (перукарня / манікюр / консультація).

Що робить:
  - показує послуги кнопками;
  - показує вільні дні та години (зайняті слоти ховає);
  - зберігає запис у SQLite, захищає від подвійного запису на один слот;
  - надсилає адміну повідомлення про новий запис;
  - показує клієнту його записи (/my) і дає скасувати.

Залежності: тільки стандартна бібліотека Python 3.9+ (без python-telegram-bot,
без pip install — нічого не треба купувати чи ставити).

Запуск у реальному Telegram:
  export TELEGRAM_BOT_TOKEN=123456:ABC...      # токен від @BotFather
  export ADMIN_CHAT_ID=123456789               # куди слати нові записи (не обов'язково)
  python booking_bot.py

Демо без токена (показує повний діалог у консолі — саме це показуємо клієнту):
  python booking_bot.py --demo
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

CONFIG = {
    "business": "Студія «Приклад»",
    "services": [
        {"name": "Стрижка", "duration": 60, "price": "450 ₴"},
        {"name": "Манікюр", "duration": 90, "price": "600 ₴"},
        {"name": "Консультація", "duration": 30, "price": "300 ₴"},
    ],
    "work_hours": [10, 11, 12, 13, 14, 15, 16, 17],
    "days_ahead": 7,
    "closed_weekdays": [6],  # 0 = понеділок, 6 = неділя
    "timezone_label": "Europe/Kyiv",
}


def setup_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ---------------------------------------------------------------- storage

class Storage:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS appointments ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " chat_id TEXT NOT NULL,"
            " service TEXT NOT NULL,"
            " slot TEXT NOT NULL UNIQUE,"
            " created_at TEXT NOT NULL"
            ")"
        )
        self.conn.commit()

    def taken_slots(self) -> set[str]:
        rows = self.conn.execute("SELECT slot FROM appointments").fetchall()
        return {row[0] for row in rows}

    def book(self, chat_id: str, service: str, slot: str) -> int | None:
        try:
            cur = self.conn.execute(
                "INSERT INTO appointments (chat_id, service, slot, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, service, slot, datetime.now().isoformat(timespec="seconds")),
            )
            self.conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def by_user(self, chat_id: str) -> list[tuple]:
        return self.conn.execute(
            "SELECT id, service, slot FROM appointments WHERE chat_id = ? ORDER BY slot",
            (chat_id,),
        ).fetchall()

    def cancel(self, chat_id: str, appointment_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM appointments WHERE id = ? AND chat_id = ?",
            (appointment_id, chat_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------- helpers

def slot_iso(day: date, hour: int) -> str:
    return datetime(day.year, day.month, day.day, hour, 0).isoformat(timespec="minutes")


def human_slot(slot: str) -> str:
    stamp = datetime.fromisoformat(slot)
    weekdays = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
    return f"{weekdays[stamp.weekday()]} {stamp.strftime('%d.%m')} о {stamp.strftime('%H:%M')}"


def kind_of(hour: int) -> str:
    return "ранок" if hour < 12 else "день" if hour < 17 else "вечір"


def free_days(storage: Storage, cfg: dict) -> list[date]:
    taken = storage.taken_slots()
    days = []
    today = date.today()
    now = datetime.now()
    for offset in range(cfg["days_ahead"]):
        day = today + timedelta(days=offset)
        if day.weekday() in cfg["closed_weekdays"]:
            continue
        # День показуємо лише тоді, коли на ньому лишився слот у майбутньому
        # (інакше ввечері в списку з'являвся б день без жодного вільного часу).
        if any(
            datetime(day.year, day.month, day.day, hour, 0) > now
            and slot_iso(day, hour) not in taken
            for hour in cfg["work_hours"]
        ):
            days.append(day)
    return days


def free_hours(storage: Storage, cfg: dict, day: date) -> list[int]:
    taken = storage.taken_slots()
    now = datetime.now()
    hours = []
    for hour in cfg["work_hours"]:
        stamp = datetime(day.year, day.month, day.day, hour, 0)
        if stamp <= now:
            continue
        if slot_iso(day, hour) not in taken:
            hours.append(hour)
    return hours


def kb(rows: list[list[tuple[str, str]]]) -> list[list[tuple[str, str]]]:
    return rows


# ---------------------------------------------------------------- bot logic

@dataclass
class Out:
    chat_id: str
    text: str
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)
    edit: bool = False


class BookingBot:
    """Чиста логіка без мережі: вхід -> список вихідних повідомлень."""

    def __init__(self, storage: Storage, cfg: dict | None = None):
        self.storage = storage
        self.cfg = cfg or CONFIG

    def handle(self, chat_id: str, text: str | None = None, callback: str | None = None) -> list[Out]:
        text = (text or "").strip()

        if text.startswith("/start") or text == "меню":
            return [self.welcome(chat_id, text)]

        if text.startswith("/my"):
            return [self.my_list(chat_id)]

        if text.startswith("/cancel"):
            return [self.cancel_by_command(chat_id, text)]

        if text.startswith("/help"):
            return [Out(chat_id, "Запис: /start · Мої записи: /my · Скасувати: /cancel <номер>")]

        if callback:
            return self.on_callback(chat_id, callback)

        return [Out(chat_id, "Не зрозумів. Натисни /start, щоб почати запис.")]

    # --- крок 1: послуги
    def welcome(self, chat_id: str, text: str) -> Out:
        cfg = self.cfg
        buttons = []
        for index, service in enumerate(cfg["services"]):
            buttons.append(
                [
                    (
                        f"{service['name']} · {service['price']}",
                        f"svc|{index}",
                    )
                ]
            )
        who = text.replace("/start", "").strip()
        greeting = f"Вітаю{', ' + who if who else ''}! Це {cfg['business']}."
        return Out(
            chat_id,
            f"{greeting}\nОберіть послугу — і я покажу вільні години.",
            kb(buttons),
        )

    def on_callback(self, chat_id: str, data: str) -> list[Out]:
        # Розділювач "|", а не ":", бо в часі слота вже є двокрапка (10:00).
        parts = data.split("|")
        action = parts[0]

        if action == "menu":
            return [self.welcome(chat_id, "")]
        if action == "svc":
            return [self.show_days(chat_id, int(parts[1]))]
        if action == "day":
            return [self.show_hours(chat_id, parts[1], int(parts[2]))]
        if action == "slot":
            return [self.show_confirm(chat_id, parts[1], int(parts[2]))]
        if action == "confirm":
            return self.confirm(chat_id, parts[1], int(parts[2]))
        if action == "cancel":
            return [Out(chat_id, "Скасовано. Записатись знову — /start", edit=True)]
        return [Out(chat_id, "Кнопка застаріла, натисни /start")]

    # --- крок 2: дні
    def show_days(self, chat_id: str, service_index: int) -> Out:
        service = self.cfg["services"][service_index]["name"]
        days = free_days(self.storage, self.cfg)
        if not days:
            return Out(chat_id, "На найближчі дні вільних годин немає. Спробуйте пізніше.")

        weekdays = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
        buttons, row = [], []
        for day in days:
            slots = len(free_hours(self.storage, self.cfg, day))
            label = f"{weekdays[day.weekday()]} {day.strftime('%d.%m')} ({slots})"
            row.append((label, f"day|{day.isoformat()}|{service_index}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([("↩ Змінити послугу", "menu")])

        return Out(
            chat_id,
            f"{service}. Оберіть день (у дужках — вільні години):",
            kb(buttons),
        )

    # --- крок 3: години
    def show_hours(self, chat_id: str, day_iso: str, service_index: int) -> Out:
        day = date.fromisoformat(day_iso)
        hours = free_hours(self.storage, self.cfg, day)
        if not hours:
            return Out(chat_id, "На цей день годин уже немає. Оберіть інший день: /start")

        service_name = self.cfg["services"][service_index]["name"]
        buttons, row = [], []
        for hour in hours:
            row.append((f"{hour:02d}:00", f"slot|{day_iso}T{hour:02d}:00|{service_index}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([("↩ Інший день", f"svc|{service_index}")])

        label = day.strftime("%d.%m")
        return Out(chat_id, f"{service_name}, {label}. Оберіть час ({kind_of(hours[0])}):", kb(buttons))

    # --- крок 4: підтвердження
    def show_confirm(self, chat_id: str, slot: str, service_index: int) -> Out:
        service = self.cfg["services"][service_index]
        if slot in self.storage.taken_slots():
            return Out(chat_id, "Цей час щойно зайняли. Оберіть інший: /start")

        price = service["price"]
        return Out(
            chat_id,
            (
                f"Підтвердіть запис:\n\n"
                f"Послуга: {service['name']}\n"
                f"Час: {human_slot(slot)}\n"
                f"Тривалість: ~{service['duration']} хв\n"
                f"Вартість: {price}\n\n"
                f"{self.cfg['business']}"
            ),
            kb(
                [
                    [("✅ Підтвердити", f"confirm|{slot}|{service_index}")],
                    [("❌ Скасувати", "cancel")],
                ]
            ),
        )

    # --- крок 5: запис
    def confirm(self, chat_id: str, slot: str, service_index: int) -> list[Out]:
        service = self.cfg["services"][service_index]["name"]
        appointment_id = self.storage.book(chat_id, service, slot)
        if appointment_id is None:
            return [
                Out(
                    chat_id,
                    "На жаль, цей час щойно зайняли. Оберіть інший: /start",
                    edit=True,
                )
            ]

        answers = [
            Out(
                chat_id,
                (
                    f"✅ Готово! Ви записані.\n\n"
                    f"№{appointment_id} · {service}\n"
                    f"{human_slot(slot)}\n\n"
                    f"Скасувати: /cancel {appointment_id}\n"
                    f"Мої записи: /my"
                ),
                edit=True,
            )
        ]
        admin = os.environ.get("ADMIN_CHAT_ID")
        if admin:
            answers.append(
                Out(
                    admin,
                    f"Новий запис №{appointment_id}\n{service}\n{human_slot(slot)}\nchat_id: {chat_id}",
                )
            )
        return answers

    def my_list(self, chat_id: str) -> Out:
        rows = self.storage.by_user(chat_id)
        if not rows:
            return Out(chat_id, "У вас поки немає записів. Записатись: /start")
        lines = [f"Ваші записи ({len(rows)}):"]
        for appointment_id, service, slot in rows:
            lines.append(f"№{appointment_id} · {service} · {human_slot(slot)}")
        lines.append("\nСкасувати: /cancel <номер>")
        return Out(chat_id, "\n".join(lines))

    def cancel_by_command(self, chat_id: str, text: str) -> Out:
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            return Out(chat_id, "Вкажіть номер запису: /cancel 3. Список — /my")
        appointment_id = int(parts[1])
        if self.storage.cancel(chat_id, appointment_id):
            return Out(chat_id, f"Запис №{appointment_id} скасовано.")
        return Out(chat_id, "Не знайшов такого запису серед ваших. Список — /my")


# ---------------------------------------------------------------- transports

class ConsoleTransport:
    """Демонстраційний транспорт: друкує діалог так, як його бачить клієнт."""

    def __init__(self):
        self.inbox: list[dict] = []

    def deliver(self, out: Out) -> None:
        print(f"\n← БОТ [chat {out.chat_id}]:")
        for line in out.text.splitlines():
            print(f"   {line}")
        if out.buttons:
            print("   [", " | ".join(label for row in out.buttons for label, _ in row), "]")


class TelegramTransport:
    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, payload: dict) -> dict:
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(self.API.format(token=self.token, method=method), data=data)
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def deliver(self, out: Out) -> None:
        payload = {
            "chat_id": out.chat_id,
            "text": out.text,
            "parse_mode": "HTML",
        }
        if out.buttons:
            payload["reply_markup"] = json.dumps(
                {
                    "inline_keyboard": [
                        [{"text": label, "callback_data": data} for label, data in row]
                        for row in out.buttons
                    ]
                }
            )
        try:
            self.call("sendMessage", payload)
        except urllib.error.URLError as exc:
            print(f"[!] sendMessage: {exc}", file=sys.stderr)

    def run(self, bot: BookingBot) -> None:
        print("Бот запущений. Ctrl+C — зупинити.")
        offset = 0
        while True:
            try:
                updates = self.call("getUpdates", {"timeout": 30, "offset": offset}).get("result", [])
            except (urllib.error.URLError, TimeoutError) as exc:
                print(f"[!] getUpdates: {exc}", file=sys.stderr)
                time.sleep(3)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                callback = update.get("callback_query") or {}
                chat_id = str(
                    (message.get("chat") or {}).get("id")
                    or ((callback.get("message") or {}).get("chat") or {}).get("id")
                    or ""
                )
                if not chat_id:
                    continue

                if message:
                    name = (message.get("from") or {}).get("first_name", "")
                    text = message.get("text", "")
                    if text.startswith("/start") and name:
                        text = f"/start {name}"
                else:
                    text = None
                    self.call("answerCallbackQuery", {"callback_query_id": callback.get("id", "")})

                for out in bot.handle(chat_id, text=text, callback=callback.get("data")):
                    self.deliver(out)


# ---------------------------------------------------------------- demo

def run_demo(bot: BookingBot) -> int:
    transport = ConsoleTransport()

    def step(text: str | None = None, callback: str | None = None, label: str = "") -> list[Out]:
        shown = text or f"[кнопка] {label or callback}"
        print(f"\n→ КЛІЄНТ: {shown}")
        outs = bot.handle("demo-1", text=text, callback=callback)
        for out in outs:
            transport.deliver(out)
        return outs

    print("=" * 62)
    print("ДЕМО: запис клієнта в Telegram-боті (без токена, без мережі)")
    print("=" * 62)

    step("/start Олена")
    step(callback="svc|1", label="Манікюр · 600 ₴")

    days = free_days(bot.storage, bot.cfg)
    if not days:
        print("[!] Немає вільних днів — демо неможливе")
        return 1
    day = days[0]
    step(callback=f"day|{day.isoformat()}|1", label=f"день {day.isoformat()}")

    hours = free_hours(bot.storage, bot.cfg, day)
    hour = hours[0]
    slot = slot_iso(day, hour)
    step(callback=f"slot|{slot}|1", label=f"{hour:02d}:00")
    step(callback=f"confirm|{slot}|1", label="✅ Підтвердити")

    print("\n--- перевірка: другий клієнт хоче той самий час ---")
    print("\n→ КЛІЄНТ 2: [кнопка] Підтвердити (той самий слот)")
    outs = bot.handle("demo-2", callback=f"confirm|{slot}|1")
    for out in outs:
        transport.deliver(out)

    step("/my")
    step("/cancel 1")

    remaining = bot.storage.by_user("demo-1")
    print(f"\nПісля скасування записів у клієнта: {len(remaining)}")
    print("Демо завершено. Реальний запуск: TELEGRAM_BOT_TOKEN=... python booking_bot.py")
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_stdout()
    parser = argparse.ArgumentParser(description="Telegram-бот запису на послуги")
    parser.add_argument("--demo", action="store_true", help="показати діалог без токена")
    parser.add_argument("--db", default=":memory:" if "--demo" in (argv or sys.argv) else "state/bookings.db")
    args = parser.parse_args(argv)

    if not args.demo and not args.db.startswith(":"):
        os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)

    storage = Storage(args.db)
    bot = BookingBot(storage)

    try:
        if args.demo:
            return run_demo(bot)

        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            print(
                "Немає TELEGRAM_BOT_TOKEN.\n"
                "1) напишіть @BotFather → /newbot → скопіюйте токен\n"
                "2) export TELEGRAM_BOT_TOKEN=...\n"
                "3) python booking_bot.py\n\n"
                "Або подивіться демо без токена: python booking_bot.py --demo",
                file=sys.stderr,
            )
            return 2
        TelegramTransport(token).run(bot)
        return 0
    except KeyboardInterrupt:
        print("\nЗупинено.")
        return 0
    finally:
        storage.close()


if __name__ == "__main__":
    raise SystemExit(main())
