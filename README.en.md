# freelance-automation-demos

[![tests](https://github.com/rmikitin2015-dev/python-automation-demos/workflows/tests/badge.svg)](https://github.com/rmikitin2015-dev/python-automation-demos/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Five small Python tools I wrote myself: a procurement monitor, a bank-statement report, a Telegram booking bot, a company finder for cold outreach, and a job-feed monitor with Telegram alerts.

Standard library only — Python 3.10+, no third-party packages, no API keys, no paid services. Two monitors read public web pages; the rest run offline. All five can run without network access: the three network tools ship a `--demo` mode backed by saved real responses.

**What I take on:** Python automation — web monitoring and scraping, reports from statements and spreadsheets, Telegram bots, API integrations.
**Contact:** [r.mikitin2015@gmail.com](mailto:r.mikitin2015@gmail.com) · [GitHub](https://github.com/rmikitin2015-dev)

[Ukrainian README](README.md) · [Case studies (UA)](docs/cases.md)

## What is here

| File | What it does | Output |
|---|---|---|
| `prozorro_monitor.py` | Reads the public Prozorro API, filters tenders by keywords, CPV code, amount and region, deduplicates | CSV, HTML report, Telegram alerts |
| `bank_report.py` | Parses a bank CSV statement, groups transactions by category, computes totals | `summary.txt`, `report.csv`, `report.html` |
| `booking_bot.py` | Telegram booking bot: free slots, confirmation, client-side cancellation, double-booking protection | SQLite records + admin list |
| `prospect_finder.py` | Takes procurement winners, aggregates amounts and win counts per company | `prospects.csv`, `prospects.html` |
| `freelance_monitor.py` | Reads the latest freelance.ua job pages and filters locally by keyword, budget and freshness; deduplicates | CSV, HTML report, Telegram alerts |

## Quick start

```bash
git clone <this repo>
cd freelance-automation-demos

python bank_report.py sample_statement.csv          # -> out/summary.txt, report.csv, report.html
python booking_bot.py --demo                        # full booking dialogue, no token
python prozorro_monitor.py -k ремонт --inspect 60 --csv out/tenders.csv --html out/tenders.html
python prospect_finder.py --days 21 --limit 200 --min-amount 100000 --top 40
python freelance_monitor.py -k python --min-budget 900 --pages 3 --all
```

## Offline demo

The three network scripts accept `--demo`: they read saved responses from `fixtures/` and go through the same path — parsing, filters, deduplication, report. Nothing is downloaded and nothing is sent.

```bash
python freelance_monitor.py --demo --all --html out/demo/orders.html
python prozorro_monitor.py --demo --all --csv out/demo/tenders.csv --html out/demo/tenders.html
python prospect_finder.py --demo --out out/demo/prospects
```

## Tests

```bash
python -m unittest discover -s tests -v
```

28 tests, no network: 3.10, 3.12 and 3.13 in GitHub Actions on every push and pull request.

## Limitations (honest)

- `prozorro_monitor.py` inspects the newest `--inspect` cards per run (100 by default); full coverage needs a deeper feed walk or the official OCDS dataset.
- `booking_bot.py` keeps dialogue state in process memory; records live in SQLite. Several masters or branches need a separate services-per-provider table.
- `bank_report.py` categorises by keywords, not by understanding the transaction description; extend `category_rules.json` for clean results.
- Prozorro does not publish company contacts — `prospect_finder.py` gives name, EDRPOU code and amounts; website/phone must be found separately.
- `freelance_monitor.py` reads only the public job list (no site search — filtering is fully local) and does not place bids: submitting proposals requires an account, so a human does it. The script finds jobs and pushes them to Telegram so the bid can be the first one.
- Orders without a stated price ("Negotiable") are **not** dropped by the budget filter: the price is unknown, not low, so they always pass if keywords and freshness match.

## License

MIT — see [LICENSE](LICENSE).
