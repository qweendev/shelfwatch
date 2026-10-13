from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shelfwatch.bot import BotNotifier, create_router
from shelfwatch.config import Settings
from shelfwatch.fetch import Fetcher
from shelfwatch.store import Base, Store
from shelfwatch.watcher import Watcher


async def main() -> None:
    settings = Settings()

    database = make_url(settings.database_url)
    if database.get_backend_name() == "sqlite" and database.database:
        Path(database.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(database)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    store = Store(async_sessionmaker(engine, expire_on_commit=False))

    headers = {"User-Agent": settings.user_agent, "Accept-Language": "en,ru;q=0.8"}
    async with httpx.AsyncClient(headers=headers, timeout=20) as client:
        fetcher = Fetcher(client, per_host_interval=settings.per_host_interval)
        bot = Bot(
            settings.bot_token.get_secret_value(),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
        )
        watcher = Watcher(
            store,
            fetcher,
            BotNotifier(bot),
            interval=timedelta(seconds=settings.check_interval),
        )
        dispatcher = Dispatcher(store=store, fetcher=fetcher, settings=settings)
        dispatcher.include_router(create_router())

        watching = asyncio.create_task(watcher.run())
        try:
            await dispatcher.start_polling(bot)
        finally:
            watching.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watching
            await bot.session.close()
            await engine.dispose()


def run() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(main())


if __name__ == "__main__":
    run()
