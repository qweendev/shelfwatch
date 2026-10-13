from __future__ import annotations

from decimal import Decimal
from html import escape

from shelfwatch.extract import Availability, Product

RUSSIAN_SPEAKING = ("ru", "uk", "be", "kk")

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "start": (
            "Send me a link to a product page. "
            "I'll message you when it's back in stock or gets cheaper.\n\n"
            "/list: what I'm watching"
        ),
        "checking": "Checking the page…",
        "unsafe_url": "I can't watch this link.",
        "fetch_failed": "Couldn't open the page: the shop didn't respond or blocked the request.",
        "not_a_product": "No price or stock info on that page. Is it a product page?",
        "limit": "You're already watching {limit} items. Remove something in /list first.",
        "watching": "Watching. I'll write when something changes.",
        "list_empty": "Nothing here yet. Send me a product link.",
        "list_title": "<b>Watching</b>",
        "target_prompt": "Send the price you're waiting for{currency}.",
        "target_set": "Got it. I'll write when the price is {price} or lower.",
        "target_cleared": "Target price removed.",
        "target_invalid": "That doesn't look like a price. Try again, e.g. 1499.",
        "removed": "Stopped watching.",
        "gone": "This item is no longer on your list.",
        "back_in_stock": "Back in stock",
        "target_reached": "Target price reached: {old} → {new}",
        "price_drop": "Price dropped: {old} → {new}",
        "unreadable": "This page failed to load several times in a row. I'll check it less often.",
        "target": "Target: {price}",
        "btn_target": "Target price",
        "btn_remove": "Stop watching",
        "btn_open": "Open",
        Availability.IN_STOCK: "In stock",
        Availability.PREORDER: "Pre-order",
        Availability.OUT_OF_STOCK: "Out of stock",
        Availability.UNKNOWN: "Stock unknown",
    },
    "ru": {
        "start": (
            "Пришли ссылку на страницу товара. "
            "Напишу, когда он появится в наличии или подешевеет.\n\n"
            "/list: что я отслеживаю"
        ),
        "checking": "Смотрю страницу…",
        "unsafe_url": "Эту ссылку отслеживать нельзя.",
        "fetch_failed": "Не открылась страница: магазин не ответил или заблокировал запрос.",
        "not_a_product": "На странице нет ни цены, ни наличия. Это точно страница товара?",
        "limit": "Ты уже следишь за {limit} товарами. Сначала удали что-нибудь в /list.",
        "watching": "Слежу. Напишу, когда что-то изменится.",
        "list_empty": "Пока пусто. Пришли ссылку на товар.",
        "list_title": "<b>Отслеживаю</b>",
        "target_prompt": "Пришли цену, которую ждёшь{currency}.",
        "target_set": "Принято. Напишу, когда цена будет {price} или ниже.",
        "target_cleared": "Целевая цена убрана.",
        "target_invalid": "Не похоже на цену. Попробуй ещё раз, например 1499.",
        "removed": "Больше не слежу.",
        "gone": "Этого товара уже нет в списке.",
        "back_in_stock": "Снова в наличии",
        "target_reached": "Цена дошла до цели: {old} → {new}",
        "price_drop": "Цена снизилась: {old} → {new}",
        "unreadable": "Несколько раз подряд не смог прочитать страницу. Буду проверять реже.",
        "target": "Цель: {price}",
        "btn_target": "Целевая цена",
        "btn_remove": "Не следить",
        "btn_open": "Открыть",
        Availability.IN_STOCK: "В наличии",
        Availability.PREORDER: "Предзаказ",
        Availability.OUT_OF_STOCK: "Нет в наличии",
        Availability.UNKNOWN: "Наличие неизвестно",
    },
}

CURRENCY_SIGNS = {"USD": "$", "EUR": "€", "GBP": "£", "RUB": "₽", "UAH": "₴", "KZT": "₸"}


def language(code: str | None) -> str:
    return "ru" if code and code.split("-")[0] in RUSSIAN_SPEAKING else "en"


def t(lang: str, key: str, **values: object) -> str:
    return TEXTS[lang][key].format(**values)


def money(amount: Decimal | None, currency: str | None) -> str:
    if amount is None:
        return "—"
    number = f"{amount:,.2f}".replace(",", " ").removesuffix(".00")
    if currency is None:
        return number
    sign = CURRENCY_SIGNS.get(currency.upper())
    return f"{sign}{number}" if sign in ("$", "£") else f"{number} {sign or currency}"


def card(
    lang: str, product: Product, url: str, target: Decimal | None = None, headline: str = ""
) -> str:
    title = escape(product.title or url)
    lines = [f"<b>{headline}</b>"] if headline else []
    lines.append(f'<a href="{escape(url)}">{title}</a>')
    lines.append(f"{money(product.price, product.currency)} · {t(lang, product.availability)}")
    if target is not None:
        lines.append(t(lang, "target", price=money(target, product.currency)))
    return "\n".join(lines)
