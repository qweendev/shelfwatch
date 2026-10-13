"""Read price and stock from the structured data shops publish for search engines."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from selectolax.lexbor import LexborHTMLParser


class Availability(StrEnum):
    IN_STOCK = "in_stock"
    PREORDER = "preorder"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"


_AVAILABILITY = {
    "instock": Availability.IN_STOCK,
    "instoreonly": Availability.IN_STOCK,
    "onlineonly": Availability.IN_STOCK,
    "limitedavailability": Availability.IN_STOCK,
    "preorder": Availability.PREORDER,
    "presale": Availability.PREORDER,
    "backorder": Availability.PREORDER,
    "outofstock": Availability.OUT_OF_STOCK,
    "oos": Availability.OUT_OF_STOCK,
    "soldout": Availability.OUT_OF_STOCK,
    "discontinued": Availability.OUT_OF_STOCK,
}

# When offers disagree (several sellers, variants), the most available one wins.
_AVAILABILITY_RANK = list(Availability)


@dataclass(frozen=True, slots=True)
class Product:
    title: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    availability: Availability = Availability.UNKNOWN
    image: str | None = None

    @property
    def is_trackable(self) -> bool:
        return self.price is not None or self.availability is not Availability.UNKNOWN

    def or_else(self, fallback: Product) -> Product:
        return Product(
            title=self.title or fallback.title,
            price=self.price if self.price is not None else fallback.price,
            currency=self.currency or fallback.currency,
            availability=(
                self.availability
                if self.availability is not Availability.UNKNOWN
                else fallback.availability
            ),
            image=self.image or fallback.image,
        )


def extract(page: str) -> Product:
    tree = LexborHTMLParser(page)
    product = Product()
    for source in (_from_json_ld, _from_microdata, _from_open_graph, _from_title):
        product = product.or_else(source(tree))
    return product


def parse_price(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    text = str(value).strip()
    if not re.fullmatch(r"\d+(\.\d+)?", text):
        text = _normalize_separators(re.sub(r"[^\d.,]", "", text))
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_availability(value: object) -> Availability:
    if not isinstance(value, str):
        return Availability.UNKNOWN
    key = re.sub(r"[\s_-]", "", value.rsplit("/", 1)[-1].lower())
    return _AVAILABILITY.get(key, Availability.UNKNOWN)


def _normalize_separators(text: str) -> str:
    text = text.strip(".,")
    dot, comma = text.rfind("."), text.rfind(",")
    if dot != -1 and comma != -1:
        decimal, thousands = (".", ",") if dot > comma else (",", ".")
        return text.replace(thousands, "").replace(decimal, ".")
    separator = "." if dot != -1 else "," if comma != -1 else None
    if separator is None:
        return text
    head, _, tail = text.rpartition(separator)
    if text.count(separator) > 1 or len(tail) == 3:
        return text.replace(separator, "")
    return f"{head}.{tail}"


def _from_json_ld(tree: LexborHTMLParser) -> Product:
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except json.JSONDecodeError:
            continue
        for item in _walk(data):
            if _is_product(item):
                return _product_from_json_ld(item)
    return Product()


def _walk(data: Any) -> Iterator[dict[str, Any]]:
    if isinstance(data, list):
        for item in data:
            yield from _walk(item)
    elif isinstance(data, dict):
        yield data
        for value in data.values():
            yield from _walk(value)


def _is_product(item: dict[str, Any]) -> bool:
    kind = item.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return "Product" in kinds and "offers" in item


def _product_from_json_ld(item: dict[str, Any]) -> Product:
    offers = list(_offers(item["offers"]))
    priced = [(p, o) for o in offers if (p := parse_price(_offer_price(o))) is not None]
    price, offer = min(priced, key=lambda pair: pair[0]) if priced else (None, {})
    availabilities = {parse_availability(offer.get("availability")) for offer in offers}
    name = item.get("name")
    return Product(
        title=html.unescape(name).strip() if isinstance(name, str) else None,
        price=price,
        currency=offer.get("priceCurrency") or _first_currency(offers),
        availability=min(
            availabilities, key=_AVAILABILITY_RANK.index, default=Availability.UNKNOWN
        ),
        image=_image_url(item.get("image")),
    )


def _offers(value: Any) -> Iterator[dict[str, Any]]:
    for offer in value if isinstance(value, list) else [value]:
        if isinstance(offer, dict):
            yield offer
            if "offers" in offer:
                yield from _offers(offer["offers"])


def _offer_price(offer: dict[str, Any]) -> Any:
    for key in ("price", "lowPrice"):
        if offer.get(key) not in (None, ""):
            return offer[key]
    spec = offer.get("priceSpecification")
    if isinstance(spec, list):
        spec = next(iter(spec), None)
    return spec.get("price") if isinstance(spec, dict) else None


def _first_currency(offers: list[dict[str, Any]]) -> str | None:
    return next((o["priceCurrency"] for o in offers if o.get("priceCurrency")), None)


def _image_url(value: Any) -> str | None:
    if isinstance(value, list):
        value = next(iter(value), None)
    if isinstance(value, dict):
        value = value.get("url")
    return value if isinstance(value, str) else None


def _from_microdata(tree: LexborHTMLParser) -> Product:
    scope = tree.css_first('[itemtype*="schema.org/Product"]')
    if scope is None:
        return Product()

    def prop(name: str) -> str | None:
        node = scope.css_first(f'[itemprop="{name}"]')
        if node is None:
            return None
        attrs = node.attributes
        return attrs.get("content") or attrs.get("href") or node.text(strip=True) or None

    return Product(
        title=prop("name"),
        price=parse_price(prop("price")),
        currency=prop("priceCurrency"),
        availability=parse_availability(prop("availability")),
        image=prop("image"),
    )


def _from_open_graph(tree: LexborHTMLParser) -> Product:
    def meta(*names: str) -> str | None:
        for name in names:
            node = tree.css_first(f'meta[property="{name}"], meta[name="{name}"]')
            content = node.attributes.get("content") if node is not None else None
            if content:
                return content
        return None

    return Product(
        title=meta("og:title"),
        price=parse_price(meta("product:price:amount", "og:price:amount")),
        currency=meta("product:price:currency", "og:price:currency"),
        availability=parse_availability(meta("product:availability", "og:availability")),
        image=meta("og:image"),
    )


def _from_title(tree: LexborHTMLParser) -> Product:
    node = tree.css_first("title")
    return Product(title=node.text(strip=True) or None if node else None)
