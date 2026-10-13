from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.methods.base import TelegramType
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update, User
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shelfwatch.bot import Action, WatchCallback, create_router
from shelfwatch.config import Settings
from shelfwatch.extract import Availability, Product
from shelfwatch.fetch import FetchError
from shelfwatch.store import Base, Store

CHAT = Chat(id=42, type="private")
USER = User(id=42, is_bot=False, first_name="Q", language_code="ru")
URL = "https://shop.test/kettle"


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109  signature comes from BaseSession
    ) -> TelegramType:
        self.calls.append(method)
        if isinstance(method, SendMessage | EditMessageText):
            reply = message(text=method.text if isinstance(method.text, str) else "")
            return reply.as_(bot)  # type: ignore[return-value]
        return True  # type: ignore[return-value]

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes]:
        raise NotImplementedError
        yield b""

    async def close(self) -> None:
        return None


class StubFetcher:
    def __init__(self, result: Product | Exception) -> None:
        self.result = result

    async def product(self, url: str) -> Product:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def message(text: str, entities: list[MessageEntity] | None = None) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=CHAT,
        from_user=USER,
        text=text,
        entities=entities,
    )


def link(text: str = f"смотри {URL}") -> Message:
    offset = text.index("http")
    return message(text, [MessageEntity(type="url", offset=offset, length=len(URL))])


@pytest.fixture
async def store() -> AsyncIterator[Store]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield Store(async_sessionmaker(engine, expire_on_commit=False))
    await engine.dispose()


class Harness:
    def __init__(self, store: Store, fetcher: StubFetcher) -> None:
        self.session = RecordingSession()
        self.bot = Bot(
            "42:TEST", session=self.session, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
        )
        self.dispatcher = Dispatcher(
            store=store, fetcher=fetcher, settings=Settings(bot_token="42:TEST")
        )
        self.dispatcher.include_router(create_router())
        self.update_id = 0

    async def send(self, event: Message | CallbackQuery) -> None:
        self.update_id += 1
        update = (
            Update(update_id=self.update_id, message=event)
            if isinstance(event, Message)
            else Update(update_id=self.update_id, callback_query=event)
        )
        await self.dispatcher.feed_update(self.bot, update)

    def texts(self) -> list[str]:
        return [
            str(call.text)
            for call in self.session.calls
            if isinstance(call, SendMessage | EditMessageText)
        ]


def press(action: Action, watch_id: int) -> CallbackQuery:
    return CallbackQuery(
        id="cb",
        from_user=USER,
        chat_instance="ci",
        message=message("card"),
        data=WatchCallback(action=action, watch_id=watch_id).pack(),
    )


KETTLE = Product(
    title="Чайник", price=Decimal(2490), currency="RUB", availability=Availability.OUT_OF_STOCK
)


async def test_link_becomes_a_watch_with_a_card(store: Store) -> None:
    harness = Harness(store, StubFetcher(KETTLE))
    await harness.send(link())

    [watch] = await store.for_chat(CHAT.id)
    assert watch.url == URL
    assert watch.lang == "ru"
    card = harness.texts()[-1]
    assert "Чайник" in card
    assert "2\u202f490 ₽" in card
    assert "Нет в наличии" in card


async def test_target_price_dialog(store: Store) -> None:
    harness = Harness(store, StubFetcher(KETTLE))
    await harness.send(link())
    [watch] = await store.for_chat(CHAT.id)

    await harness.send(press(Action.TARGET, watch.id))
    await harness.send(message("дорого"))
    await harness.send(message("1 999"))

    assert (await store.get(CHAT.id, watch.id)).target_price == Decimal(1999)  # type: ignore[union-attr]
    assert harness.texts()[-1].startswith("Принято")


async def test_remove_from_list(store: Store) -> None:
    harness = Harness(store, StubFetcher(KETTLE))
    await harness.send(link())
    [watch] = await store.for_chat(CHAT.id)

    await harness.send(press(Action.REMOVE_LISTED, watch.id))

    assert await store.for_chat(CHAT.id) == []
    assert any(isinstance(call, AnswerCallbackQuery) for call in harness.session.calls)
    assert harness.texts()[-1] == "Пока пусто. Пришли ссылку на товар."


async def test_remove_from_card_drops_the_buttons(store: Store) -> None:
    harness = Harness(store, StubFetcher(KETTLE))
    await harness.send(link())
    [watch] = await store.for_chat(CHAT.id)

    await harness.send(press(Action.REMOVE, watch.id))

    assert isinstance(harness.session.calls[-1], EditMessageReplyMarkup)


@pytest.mark.parametrize(
    ("result", "reply"),
    [
        (FetchError("403"), "Не открылась страница"),
        (Product(title="О нас"), "На странице нет ни цены"),
    ],
)
async def test_unreadable_links_are_explained(
    store: Store, result: Product | Exception, reply: str
) -> None:
    harness = Harness(store, StubFetcher(result))
    await harness.send(link())
    assert harness.texts()[-1].startswith(reply)
    assert await store.for_chat(CHAT.id) == []


async def test_plain_chatter_is_ignored(store: Store) -> None:
    harness = Harness(store, StubFetcher(KETTLE))
    await harness.send(message("привет"))
    assert harness.session.calls == []
