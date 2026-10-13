from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from shelfwatch.extract import Availability, Product
from shelfwatch.fetch import FetchError
from shelfwatch.store import Store, Watch

log = logging.getLogger(__name__)

FAILURES_BEFORE_ALERT = 5


class ChangeKind(StrEnum):
    BACK_IN_STOCK = "back_in_stock"
    TARGET_REACHED = "target_reached"
    PRICE_DROP = "price_drop"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class Change:
    kind: ChangeKind
    old_price: Decimal | None = None


def detect_changes(before: Product, after: Product, target: Decimal | None) -> list[Change]:
    changes = []
    was_unavailable = before.availability in (Availability.OUT_OF_STOCK, Availability.PREORDER)
    if was_unavailable and after.availability is Availability.IN_STOCK:
        changes.append(Change(ChangeKind.BACK_IN_STOCK))

    if after.price is None or before.price is None or before.currency != after.currency:
        return changes
    if target is not None and after.price <= target < before.price:
        changes.append(Change(ChangeKind.TARGET_REACHED, old_price=before.price))
    elif after.price < before.price:
        changes.append(Change(ChangeKind.PRICE_DROP, old_price=before.price))
    return changes


class ProductSource(Protocol):
    async def product(self, url: str) -> Product: ...


Notify = Callable[[Watch, Product, Change], Awaitable[None]]


class Watcher:
    def __init__(
        self,
        store: Store,
        source: ProductSource,
        notify: Notify,
        *,
        interval: timedelta,
        batch_size: int = 50,
        idle_delay: float = 30.0,
    ) -> None:
        self._store = store
        self._source = source
        self._notify = notify
        self._interval = interval
        self._batch_size = batch_size
        self._idle_delay = idle_delay

    async def run(self) -> None:
        while True:
            try:
                checked = await self.check_due()
            except Exception:
                log.exception("watch cycle failed")
                checked = 0
            if checked < self._batch_size:
                await asyncio.sleep(self._idle_delay)

    async def check_due(self) -> int:
        watches = await self._store.due(self._batch_size)
        await asyncio.gather(*(self._check(watch) for watch in watches))
        return len(watches)

    async def _check(self, watch: Watch) -> None:
        try:
            product = await self._source.product(watch.url)
        except FetchError as error:
            log.info("watch %s: %s", watch.id, error)
            product = None
        if product is None or not product.is_trackable:
            failures = await self._store.record_failure(watch.id, self._interval)
            if failures == FAILURES_BEFORE_ALERT:
                await self._send(watch, watch.snapshot(), Change(ChangeKind.UNREADABLE))
            return

        changes = detect_changes(watch.snapshot(), product, watch.target_price)
        await self._store.record_check(watch.id, product, self._interval)
        for change in changes:
            await self._send(watch, product, change)

    async def _send(self, watch: Watch, product: Product, change: Change) -> None:
        try:
            await self._notify(watch, product, change)
        except Exception:
            log.exception("watch %s: failed to deliver %s", watch.id, change.kind)
