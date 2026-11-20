# freelance-automation-demos

Чотири невеликі інструменти на Python, які я написав самостійно: моніторинг тендерів, звіт із банківської виписки, Telegram-бот запису на послуги й монітор нових замовлень на freelance.ua зі сповіщеннями в Telegram.

Тільки стандартна бібліотека Python 3.9+ — жодних сторонніх залежностей, ключів API чи платних сервісів. Крім монітора тендерів, усе працює офлайн.

## Що тут є

| Файл | Що робить | Результат |
|---|---|---|
| `prozorro_monitor.py` | Читає відкритий API Prozorro, фільтрує закупівлі за ключовими словами, кодом ДК 021, сумою й регіоном, прибирає дублі | CSV, HTML-звіт, сповіщення в Telegram |
| `bank_report.py` | Розбирає CSV-виписку банку, групує транзакції за категоріями, рахує підсумки | `summary.txt`, `report.csv`, `report.html` |
| `booking_bot.py` | Telegram-бот запису на послуги: вільні слоти, підтвердження, скасування клієнтом, захист від подвійного запису | записи в SQLite + адмін-список |
| `prospect_finder.py` | Бере переможців закупівель Prozorro, агрегує по компаніях суми й кількість перемог | `prospects.csv`, `prospects.html` |
| `freelance_monitor.py` | Читає список замовлень freelance.ua за моїми запитами, фільтрує за ключовими словами, бюджетом і свіжістю, прибирає дублі | CSV, HTML-звіт, сповіщення в Telegram |

## Швидкий старт

```bash
git clone <this repo>
cd freelance-automation-demos

# 1. Звіт по виписці
python bank_report.py sample_statement.csv
#    -> out/summary.txt, out/report.csv, out/report.html

# 2. Бот запису — повний діалог без токена
python booking_bot.py --demo

# 3. Монітор тендерів (живий API, без ключа)
python prozorro_monitor.py -k ремонт --inspect 60 --csv out/tenders.csv --html out/tenders.html
python prozorro_monitor.py --config config.example.json --inspect 100

# 4. Пошук компаній серед переможців тендерів
python prospect_finder.py --days 21 --limit 200 --min-amount 100000 --top 40
#    -> out/prospects.csv + out/prospects.html

# 5. Монітор нових замовлень freelance.ua
python freelance_monitor.py --config freelance_config.example.json --csv out/orders.csv --html out/orders.html
python freelance_monitor.py -q python -q "телеграм бот" --min-budget 900 --all
python freelance_monitor.py --loop 600 --telegram   # перевіряти кожні 10 хв
```

Реальний Telegram:

```bash
export TELEGRAM_BOT_TOKEN=123456:ABC...   # токен від @BotFather
export ADMIN_CHAT_ID=123456789            # куди слати нові записи
python booking_bot.py
```

Для обох моніторів — ті самі `TELEGRAM_BOT_TOKEN` і `TELEGRAM_CHAT_ID` плюс прапорець `--telegram`: кожне нове замовлення приходить окремим повідомленням із назвою, бюджетом, кількістю пропозицій і посиланням.

## Тести

```bash
python -m unittest discover -s tests -v
```

22 тести, без мережі: розбір сум і категорій виписки, логіка запису (включно з відмовою у подвійному записі та спробою скасувати чужий запис), фільтри тендерів, розбір HTML-сторінки замовлень freelance.ua на фікстурі, фільтри за бюджетом/свіжістю/словами, дедуплікація в SQLite, текст сповіщення, нормалізація контрактів і сортування списку компаній.

## Структура

```
bank_report.py          звіт із виписки
booking_bot.py          Telegram-бот запису
prozorro_monitor.py     монітор закупівель
prospect_finder.py      пошук компаній для контактів
freelance_monitor.py    монітор замовлень freelance.ua
category_rules.json     правила категорій для звіту
config.example.json     приклад конфігурації монітора Prozorro
freelance_config.example.json  приклад фільтрів для freelance.ua
sample_statement.csv    тестова виписка
tests/test_demos.py     тести
out/                    результати запусків
portfolio/              скріншоти для портфоліо
```

## Обмеження (чесно)

- `prozorro_monitor.py` за один прогін переглядає найновіші `--inspect` карток (типово 100). Для повного покриття потрібен глибший обхід фіду або офіційний OCDS-датасет.
- `booking_bot.py` тримає стан діалогу в памʼяті процесу, записи — в SQLite. Для кількох майстрів чи філій потрібна окрема таблиця послуг-виконавців.
- `bank_report.py` категоризує за ключовими словами, а не «розуміє» опис транзакції: для чистих результатів треба доповнити `category_rules.json` своїми мерчантами.
- Контакти компаній Prozorro не публікує — `prospect_finder.py` дає назву, ЄДРПОУ й суми, а сайт/телефон треба знайти окремо.
- `freelance_monitor.py` читає відкритий список замовлень і не робить ставок: подача пропозицій на freelance.ua потребує входу в акаунт, тому це робить людина. Скрипт лише знаходить замовлення й одразу надсилає їх у Telegram, щоб ставку можна було зробити першою.
- Розмітка freelance.ua може змінитися — тоді треба поправити регулярні вирази в `parse_orders()` (тести на фікстурі покажуть, що саме зламалося).
