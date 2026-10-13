from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shelfwatch.extract import Availability, Product
from shelfwatch.fetch import FetchError
from shelfwatch.store import Base, Store, Watch, WatchLimitReachedError, utcnow
from shelfwatch.watcher import (
    FAILURES_BEFORE_ALERT,
    Change,
    ChangeKind,
    Watcher,
    detect_changes,
)

URL = "https://shop.test/item"
INTERVAL = timedelta(minutes=15)


def item(price: str | None, availability: Availability = Availability.IN_STOCK) -> Product:
    return Product(
        title="Item",
        price=Decimal(price) if price else None,
        currency="USD",
        availability=availability,
    )


class StubSource:
    def __init__(self) -> None:
        self.result: Product | Exception = item("10")

    async def product(self, url: str) -> Product:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Outbox:
    def __init__(self) -> None:
        self.sent: list[tuple[int, ChangeKind]] = []

    async def __call__(self, watch: Watch, product: Product, change: Change) -> None:
        self.sent.append((watch.chat_id, change.kind))


@pytest.fixture
async def store() -> AsyncIterator[Store]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield Store(async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


async def make_due(store: Store) -> None:
    async with store._sessions.begin() as session:
        await session.execute(update(Watch).values(next_check_at=utcnow()))


@pytest.mark.parametrize(
    ("before", "after", "target", "expected"),
    [
        (item("10", Availability.OUT_OF_STOCK), item("10"), None, [ChangeKind.BACK_IN_STOCK]),
        (item("10", Availability.UNKNOWN), item("10"), None, []),
        (item("10"), item("8"), None, [ChangeKind.PRICE_DROP]),
        (item("10"), item("8"), Decimal(9), [ChangeKind.TARGET_REACHED]),
        (item("8"), item("7"), Decimal(9), [ChangeKind.PRICE_DROP]),
        (item("10"), item("12"), None, []),
        (item("10"), item(None), None, []),
        (
            item("10", Availability.PREORDER),
            item("5"),
            Decimal(6),
            [ChangeKind.BACK_IN_STOCK, ChangeKind.TARGET_REACHED],
        ),
    ],
)
def test_detect_changes(
    before: Product, after: Product, target: Decimal | None, expected: list[ChangeKind]
) -> None:
    assert [c.kind for c in detect_changes(before, after, target)] == expected


def test_price_drop_ignored_when_currency_changes() -> None:
    before = item("100")
    after = Product(price=Decimal(2), currency="EUR", availability=Availability.IN_STOCK)
    assert detect_changes(before, after, None) == []


async def test_watch_limit_is_per_chat(store: Store) -> None:
    for n in range(2):
        await store.add(
            chat_id=1, lang="en", url=f"{URL}/{n}", product=item("1"), limit=2, interval=INTERVAL
        )
    with pytest.raises(WatchLimitReachedError):
        await store.add(
            chat_id=1, lang="en", url=f"{URL}/3", product=item("1"), limit=2, interval=INTERVAL
        )
    await store.add(chat_id=2, lang="en", url=URL, product=item("1"), limit=2, interval=INTERVAL)


async def test_adding_same_url_twice_keeps_one_watch(store: Store) -> None:
    for _ in range(2):
        await store.add(
            chat_id=1, lang="en", url=URL, product=item("1"), limit=5, interval=INTERVAL
        )
    assert len(await store.for_chat(1)) == 1


async def test_other_chats_cannot_touch_a_watch(store: Store) -> None:
    watch = await store.add(
        chat_id=1, lang="en", url=URL, product=item("1"), limit=5, interval=INTERVAL
    )
    assert await store.remove(2, watch.id) is False
    assert await store.set_target(2, watch.id, Decimal(1)) is None
    assert await store.get(2, watch.id) is None


async def test_watcher_reports_restock_and_saves_state(store: Store) -> None:
    await store.add(
        chat_id=7,
        lang="en",
        url=URL,
        product=item("10", Availability.OUT_OF_STOCK),
        limit=5,
        interval=INTERVAL,
    )
    source, outbox = StubSource(), Outbox()
    watcher = Watcher(store, source, outbox, interval=INTERVAL)

    assert await watcher.check_due() == 0

    await make_due(store)
    assert await watcher.check_due() == 1
    assert outbox.sent == [(7, ChangeKind.BACK_IN_STOCK)]

    [watch] = await store.for_chat(7)
    assert watch.availability == Availability.IN_STOCK
    assert watch.next_check_at > utcnow()


async def test_watcher_backs_off_and_alerts_once(store: Store) -> None:
    await store.add(chat_id=7, lang="en", url=URL, product=item("10"), limit=5, interval=INTERVAL)
    source, outbox = StubSource(), Outbox()
    source.result = FetchError("blocked")
    watcher = Watcher(store, source, outbox, interval=INTERVAL)

    for _ in range(FAILURES_BEFORE_ALERT + 2):
        await make_due(store)
        await watcher.check_due()

    assert outbox.sent == [(7, ChangeKind.UNREADABLE)]
    [watch] = await store.for_chat(7)
    assert watch.next_check_at - utcnow() > INTERVAL * 16


async def test_failed_delivery_does_not_lose_the_check(store: Store) -> None:
    await store.add(chat_id=7, lang="en", url=URL, product=item("10"), limit=5, interval=INTERVAL)
    source = StubSource()
    source.result = item("5")

    async def broken(watch: Watch, product: Product, change: Change) -> None:
        raise RuntimeError("telegram is down")

    await make_due(store)
    await Watcher(store, source, broken, interval=INTERVAL).check_due()
    [watch] = await store.for_chat(7)
    assert watch.price == Decimal(5)
