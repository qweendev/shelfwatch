from __future__ import annotations

from datetime import timedelta
from enum import StrEnum

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shelfwatch.config import Settings
from shelfwatch.extract import Product, parse_price
from shelfwatch.fetch import Fetcher, FetchError, UnsafeURLError
from shelfwatch.store import Store, Watch, WatchLimitReachedError
from shelfwatch.texts import card, language, money, t
from shelfwatch.watcher import Change, ChangeKind

router = Router(name="shelfwatch")


class Action(StrEnum):
    TARGET = "target"
    REMOVE = "remove"
    REMOVE_LISTED = "remove_listed"


class WatchCallback(CallbackData, prefix="w"):
    action: Action
    watch_id: int


class TargetPrice(StatesGroup):
    waiting = State()


def watch_keyboard(lang: str, watch: Watch) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t(lang, "btn_target"),
        callback_data=WatchCallback(action=Action.TARGET, watch_id=watch.id),
    )
    builder.button(
        text=t(lang, "btn_remove"),
        callback_data=WatchCallback(action=Action.REMOVE, watch_id=watch.id),
    )
    return builder.as_markup()


def list_view(lang: str, watches: list[Watch]) -> tuple[str, InlineKeyboardMarkup | None]:
    if not watches:
        return t(lang, "list_empty"), None
    lines = [t(lang, "list_title")]
    rows = []
    for number, watch in enumerate(watches, start=1):
        snapshot = watch.snapshot()
        price = money(snapshot.price, snapshot.currency)
        lines.append(f"{number}. {price} · {t(lang, snapshot.availability)}")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{number}. {_short(watch.title or watch.url)}", url=watch.url
                ),
                InlineKeyboardButton(
                    text="✕",
                    callback_data=WatchCallback(
                        action=Action.REMOVE_LISTED, watch_id=watch.id
                    ).pack(),
                ),
            ]
        )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _short(text: str, limit: int = 32) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _lang(message: Message | CallbackQuery) -> str:
    return language(message.from_user.language_code if message.from_user else None)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(t(_lang(message), "start"))


@router.message(Command("list"))
async def show_list(message: Message, store: Store, state: FSMContext) -> None:
    await state.clear()
    text, keyboard = list_view(_lang(message), await store.for_chat(message.chat.id))
    await message.answer(text, reply_markup=keyboard)


@router.message(TargetPrice.waiting, F.text)
async def receive_target(message: Message, store: Store, state: FSMContext) -> None:
    lang = _lang(message)
    price = parse_price(message.text or "")
    if price is None:
        await message.answer(t(lang, "target_invalid"))
        return
    data = await state.get_data()
    await state.clear()
    watch = await store.set_target(message.chat.id, data["watch_id"], price or None)
    if watch is None:
        await message.answer(t(lang, "gone"))
    elif price:
        await message.answer(t(lang, "target_set", price=money(price, watch.currency)))
    else:
        await message.answer(t(lang, "target_cleared"))


@router.message(F.text)
async def add_watch(
    message: Message, store: Store, fetcher: Fetcher, settings: Settings, state: FSMContext
) -> None:
    url = _first_url(message)
    if url is None:
        return
    await state.clear()
    lang = _lang(message)
    status = await message.answer(t(lang, "checking"))
    try:
        product = await fetcher.product(url)
    except UnsafeURLError:
        await status.edit_text(t(lang, "unsafe_url"))
        return
    except FetchError:
        await status.edit_text(t(lang, "fetch_failed"))
        return
    if not product.is_trackable:
        await status.edit_text(t(lang, "not_a_product"))
        return
    try:
        watch = await store.add(
            chat_id=message.chat.id,
            lang=lang,
            url=url,
            product=product,
            limit=settings.max_watches_per_chat,
            interval=timedelta(seconds=settings.check_interval),
        )
    except WatchLimitReachedError:
        await status.edit_text(t(lang, "limit", limit=settings.max_watches_per_chat))
        return
    await status.edit_text(
        card(lang, product, url, watch.target_price, headline=t(lang, "watching")),
        reply_markup=watch_keyboard(lang, watch),
    )


@router.callback_query(WatchCallback.filter(F.action == Action.TARGET))
async def ask_target(
    query: CallbackQuery, callback_data: WatchCallback, store: Store, state: FSMContext
) -> None:
    lang = _lang(query)
    if query.message is None:
        return
    watch = await store.get(query.message.chat.id, callback_data.watch_id)
    if watch is None:
        await query.answer(t(lang, "gone"))
        return
    await state.set_state(TargetPrice.waiting)
    await state.update_data(watch_id=watch.id)
    currency = f" ({watch.currency})" if watch.currency else ""
    await query.message.answer(t(lang, "target_prompt", currency=currency))
    await query.answer()


@router.callback_query(WatchCallback.filter(F.action.in_({Action.REMOVE, Action.REMOVE_LISTED})))
async def remove_watch(query: CallbackQuery, callback_data: WatchCallback, store: Store) -> None:
    lang = _lang(query)
    message = query.message
    if not isinstance(message, Message):
        await query.answer()
        return
    removed = await store.remove(message.chat.id, callback_data.watch_id)
    await query.answer(t(lang, "removed" if removed else "gone"))
    if callback_data.action is Action.REMOVE_LISTED:
        text, keyboard = list_view(lang, await store.for_chat(message.chat.id))
        await message.edit_text(text, reply_markup=keyboard)
    else:
        await message.edit_reply_markup(reply_markup=None)


def _first_url(message: Message) -> str | None:
    for entity in message.entities or []:
        if entity.type == "text_link" and entity.url:
            return entity.url
        if entity.type == "url" and message.text:
            return entity.extract_from(message.text)
    return None


class BotNotifier:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def __call__(self, watch: Watch, product: Product, change: Change) -> None:
        lang = watch.lang
        if change.kind is ChangeKind.UNREADABLE:
            headline = t(lang, "unreadable")
        elif change.kind is ChangeKind.BACK_IN_STOCK:
            headline = t(lang, "back_in_stock")
        else:
            headline = t(
                lang,
                change.kind,
                old=money(change.old_price, product.currency),
                new=money(product.price, product.currency),
            )
        await self._bot.send_message(
            watch.chat_id,
            card(lang, product, watch.url, watch.target_price, headline=headline),
            reply_markup=watch_keyboard(lang, watch),
        )
