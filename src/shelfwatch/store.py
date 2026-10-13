from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Dialect, String, UniqueConstraint, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from shelfwatch.extract import Availability, Product

MAX_BACKOFF_STEPS = 5


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class DecimalString(TypeDecorator[Decimal]):
    """Exact prices on every backend, including SQLite which has no decimal type."""

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> str | None:
        return None if value is None else str(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        return None if value is None else Decimal(value)


class Base(DeclarativeBase):
    pass


class Watch(Base):
    __tablename__ = "watches"
    __table_args__ = (UniqueConstraint("chat_id", "url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    lang: Mapped[str] = mapped_column(String(8))
    url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str | None] = mapped_column(String(512))
    price: Mapped[Decimal | None] = mapped_column(DecimalString)
    currency: Mapped[str | None] = mapped_column(String(8))
    availability: Mapped[Availability] = mapped_column(String(16))
    target_price: Mapped[Decimal | None] = mapped_column(DecimalString)
    failures: Mapped[int] = mapped_column(default=0)
    checked_at: Mapped[datetime] = mapped_column(default=utcnow)
    next_check_at: Mapped[datetime] = mapped_column(index=True)

    def snapshot(self) -> Product:
        return Product(
            title=self.title,
            price=self.price,
            currency=self.currency,
            availability=Availability(self.availability),
        )

    def apply(self, product: Product) -> None:
        self.title = (product.title or self.title or "")[:512] or None
        self.price = product.price
        self.currency = product.currency
        self.availability = product.availability


class WatchLimitReachedError(Exception):
    pass


class Store:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def add(
        self,
        *,
        chat_id: int,
        lang: str,
        url: str,
        product: Product,
        limit: int,
        interval: timedelta,
    ) -> Watch:
        async with self._sessions.begin() as session:
            watch = await session.scalar(
                select(Watch).where(Watch.chat_id == chat_id, Watch.url == url)
            )
            if watch is None:
                count = await session.scalar(
                    select(func.count()).select_from(Watch).where(Watch.chat_id == chat_id)
                )
                if (count or 0) >= limit:
                    raise WatchLimitReachedError
                watch = Watch(chat_id=chat_id, url=url)
                session.add(watch)
            watch.lang = lang
            watch.apply(product)
            watch.failures = 0
            watch.checked_at = utcnow()
            watch.next_check_at = watch.checked_at + interval
        return watch

    async def for_chat(self, chat_id: int) -> list[Watch]:
        async with self._sessions() as session:
            result = await session.scalars(
                select(Watch).where(Watch.chat_id == chat_id).order_by(Watch.id)
            )
            return list(result)

    async def get(self, chat_id: int, watch_id: int) -> Watch | None:
        async with self._sessions() as session:
            watch = await session.get(Watch, watch_id)
            return watch if watch is not None and watch.chat_id == chat_id else None

    async def remove(self, chat_id: int, watch_id: int) -> bool:
        async with self._sessions.begin() as session:
            watch = await session.get(Watch, watch_id)
            if watch is None or watch.chat_id != chat_id:
                return False
            await session.delete(watch)
            return True

    async def set_target(self, chat_id: int, watch_id: int, price: Decimal | None) -> Watch | None:
        async with self._sessions.begin() as session:
            watch = await session.get(Watch, watch_id)
            if watch is None or watch.chat_id != chat_id:
                return None
            watch.target_price = price
            return watch

    async def due(self, limit: int) -> list[Watch]:
        async with self._sessions() as session:
            result = await session.scalars(
                select(Watch)
                .where(Watch.next_check_at <= utcnow())
                .order_by(Watch.next_check_at)
                .limit(limit)
            )
            return list(result)

    async def record_check(self, watch_id: int, product: Product, interval: timedelta) -> None:
        async with self._sessions.begin() as session:
            watch = await session.get(Watch, watch_id)
            if watch is None:
                return
            watch.apply(product)
            watch.failures = 0
            watch.checked_at = utcnow()
            watch.next_check_at = watch.checked_at + interval

    async def record_failure(self, watch_id: int, interval: timedelta) -> int:
        """Retry broken pages exponentially less often; returns the failure streak."""
        async with self._sessions.begin() as session:
            watch = await session.get(Watch, watch_id)
            if watch is None:
                return 0
            watch.failures += 1
            backoff = 2 ** min(watch.failures, MAX_BACKOFF_STEPS)
            watch.next_check_at = utcnow() + interval * backoff
            return watch.failures
