# shelfwatch

[![ci](https://github.com/qweendev/shelfwatch/actions/workflows/ci.yml/badge.svg)](https://github.com/qweendev/shelfwatch/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.13-0B0F14)
![license](https://img.shields.io/badge/license-MIT-0B0F14)

A Telegram bot that watches product pages and messages you when an item is back in stock or gets cheaper.
Send it a link. That is the whole setup.

## Why

[inventory-hunter](https://github.com/EricJMarti/inventory-hunter) got a lot of people their GPUs in 2020,
then stopped working: every shop needed a hand-written YAML file with CSS selectors that broke on the next
redesign, and a headless Chrome inside Docker that kept crashing.

shelfwatch takes a different route. Shops already publish machine-readable product data for search engines
(schema.org JSON-LD, microdata, OpenGraph). Reading that instead of the page layout means no per-shop config,
no browser, and nothing to fix when a shop changes its design.

## How it works

```
link ─► fetch ─► extract ─► store ─► watcher (every 15 min) ─► diff ─► Telegram
         │          │                    │
         │          │                    └─ exponential backoff for pages that keep failing
         │          └─ JSON-LD → microdata → OpenGraph, cheapest offer, best availability
         └─ public hosts only (redirects re-checked), per-shop rate limit, 5 MB cap
```

You get a message when:

- an item that was out of stock or on pre-order is available again
- the price drops
- the price falls to the target you set

## Run

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. `cp .env.example .env` and paste the token.
3. `docker compose up -d`

Without Docker: `uv sync && uv run shelfwatch`.

| variable | default | |
|---|---|---|
| `SHELFWATCH_BOT_TOKEN` | — | required |
| `SHELFWATCH_DATABASE_URL` | `sqlite+aiosqlite:///data/shelfwatch.db` | any SQLAlchemy async URL |
| `SHELFWATCH_CHECK_INTERVAL` | `900` | seconds between checks of one item |
| `SHELFWATCH_PER_HOST_INTERVAL` | `5` | seconds between requests to the same shop |
| `SHELFWATCH_MAX_WATCHES_PER_CHAT` | `25` | |

## What it can and can't watch

Works with shops that publish product data: Shopify, WooCommerce, Magento and most mid-sized stores.

Doesn't work yet with pages rendered entirely in JavaScript and with marketplaces that block automated
requests (Amazon, Ozon, Wildberries and similar). Those need dedicated adapters; see the issues.

## Development

```
uv sync
uv run pytest
uv run ruff check . && uv run mypy src tests
```

## License

MIT © 2026 [qweendev](https://github.com/qweendev)
