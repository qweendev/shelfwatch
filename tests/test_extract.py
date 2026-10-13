from decimal import Decimal

import pytest

from shelfwatch.extract import Availability, extract, parse_availability, parse_price


def page(head: str = "", body: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


def json_ld(payload: str) -> str:
    return f'<script type="application/ld+json">{payload}</script>'


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1299", Decimal(1299)),
        ("19.99", Decimal("19.99")),
        (19.99, Decimal("19.99")),
        (1299, Decimal(1299)),
        ("$1,299.00", Decimal("1299.00")),
        ("1.299,50 €", Decimal("1299.50")),
        ("1 299 ₽", Decimal(1299)),
        ("12 990,00\u00a0₽", Decimal("12990.00")),
        ("1.299 €", Decimal(1299)),
        ("1,5", Decimal("1.5")),
        ("1.234.567", Decimal(1234567)),
        ("49,-", Decimal(49)),
        ("", None),
        ("call us", None),
        (None, None),
        (True, None),
    ],
)
def test_parse_price(raw: object, expected: Decimal | None) -> None:
    assert parse_price(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://schema.org/InStock", Availability.IN_STOCK),
        ("http://schema.org/OutOfStock", Availability.OUT_OF_STOCK),
        ("PreOrder", Availability.PREORDER),
        ("in stock", Availability.IN_STOCK),
        ("out_of_stock", Availability.OUT_OF_STOCK),
        ("oos", Availability.OUT_OF_STOCK),
        ("whatever", Availability.UNKNOWN),
        (None, Availability.UNKNOWN),
    ],
)
def test_parse_availability(raw: object, expected: Availability) -> None:
    assert parse_availability(raw) is expected


def test_json_ld_product() -> None:
    product = extract(
        page(
            json_ld("""{
                "@context": "https://schema.org", "@type": "Product",
                "name": "Keychron K2 &amp; case", "image": ["https://shop.test/k2.jpg"],
                "offers": {"@type": "Offer", "price": "89.99", "priceCurrency": "USD",
                           "availability": "https://schema.org/InStock"}
            }""")
        )
    )
    assert product.title == "Keychron K2 & case"
    assert product.price == Decimal("89.99")
    assert product.currency == "USD"
    assert product.availability is Availability.IN_STOCK
    assert product.image == "https://shop.test/k2.jpg"


def test_json_ld_inside_graph_with_broken_neighbour() -> None:
    product = extract(
        page(
            json_ld("{ not json")
            + json_ld("""{"@graph": [
                {"@type": "BreadcrumbList", "itemListElement": []},
                {"@type": ["Product", "Thing"], "name": "Lamp",
                 "offers": {"@type": "Offer", "priceSpecification": {"price": 15}}}
            ]}""")
        )
    )
    assert product.title == "Lamp"
    assert product.price == Decimal(15)


def test_json_ld_takes_cheapest_offer_and_best_availability() -> None:
    product = extract(
        page(
            json_ld("""{"@type": "Product", "name": "GPU", "offers": [
                {"price": "700", "priceCurrency": "EUR", "availability": "OutOfStock"},
                {"price": "650", "priceCurrency": "EUR", "availability": "OutOfStock"},
                {"price": "720", "priceCurrency": "EUR", "availability": "InStock"}
            ]}""")
        )
    )
    assert product.price == Decimal(650)
    assert product.availability is Availability.IN_STOCK


def test_json_ld_aggregate_offer_uses_low_price() -> None:
    product = extract(
        page(
            json_ld("""{"@type": "Product", "name": "Chair", "offers": {
                "@type": "AggregateOffer", "lowPrice": "120", "highPrice": "180",
                "priceCurrency": "GBP"
            }}""")
        )
    )
    assert product.price == Decimal(120)
    assert product.currency == "GBP"


def test_microdata_product() -> None:
    product = extract(
        page(
            body="""
            <nav itemscope itemtype="https://schema.org/BreadcrumbList">
              <span itemprop="name">Home</span>
            </nav>
            <div itemscope itemtype="https://schema.org/Product">
              <h1 itemprop="name">Kettle</h1>
              <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
                <span itemprop="price" content="2490.00">2 490 ₽</span>
                <meta itemprop="priceCurrency" content="RUB">
                <link itemprop="availability" href="https://schema.org/OutOfStock">
              </div>
            </div>"""
        )
    )
    assert product.title == "Kettle"
    assert product.price == Decimal("2490.00")
    assert product.currency == "RUB"
    assert product.availability is Availability.OUT_OF_STOCK


def test_open_graph_fills_what_json_ld_lacks() -> None:
    product = extract(
        page(
            head="""
            <title>Shop | Sneakers</title>
            <meta property="og:title" content="Sneakers">
            <meta property="product:price:amount" content="129.95">
            <meta property="product:price:currency" content="USD">
            <meta property="product:availability" content="in stock">"""
            + json_ld('{"@type": "Product", "name": "Sneakers v2", "offers": {}}')
        )
    )
    assert product.title == "Sneakers v2"
    assert product.price == Decimal("129.95")
    assert product.availability is Availability.IN_STOCK


def test_page_without_product_data() -> None:
    product = extract(page(head="<title>About us</title>", body="<p>We sell things.</p>"))
    assert product.title == "About us"
    assert not product.is_trackable
