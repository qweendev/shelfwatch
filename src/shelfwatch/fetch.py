from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable

import httpx

from shelfwatch.extract import Product, extract

MAX_REDIRECTS = 5
MAX_PAGE_BYTES = 5 * 1024 * 1024


class FetchError(Exception):
    pass


class UnsafeURLError(FetchError):
    pass


async def ensure_public_host(url: httpx.URL) -> None:
    """Refuse URLs that point into the server's own network: users control what we fetch."""
    if url.scheme not in ("http", "https") or not url.host:
        raise UnsafeURLError(f"unsupported url: {url}")
    port = url.port or (443 if url.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            url.host, port, type=socket.SOCK_STREAM
        )
    except socket.gaierror as error:
        raise FetchError(f"cannot resolve {url.host}") from error
    for *_, sockaddr in infos:
        if not ipaddress.ip_address(sockaddr[0]).is_global:
            raise UnsafeURLError(f"{url.host} resolves to a non-public address")


class Fetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        per_host_interval: float = 5.0,
        check_url: Callable[[httpx.URL], Awaitable[None]] = ensure_public_host,
    ) -> None:
        self._client = client
        self._per_host_interval = per_host_interval
        self._check_url = check_url
        self._next_slot: dict[str, float] = {}
        self._slots_lock = asyncio.Lock()

    async def product(self, url: str) -> Product:
        return extract(await self.page(url))

    async def page(self, url: str) -> str:
        target = httpx.URL(url)
        for _ in range(MAX_REDIRECTS + 1):
            await self._check_url(target)
            await self._wait_turn(target.host)
            try:
                async with self._client.stream("GET", target) as response:
                    if response.is_redirect:
                        target = target.join(response.headers["location"])
                        continue
                    response.raise_for_status()
                    return await _read_capped(response)
            except httpx.HTTPError as error:
                raise FetchError(str(error) or type(error).__name__) from error
        raise FetchError("too many redirects")

    async def _wait_turn(self, host: str) -> None:
        """Space out requests to the same shop so a long watch list never looks like a flood."""
        loop = asyncio.get_running_loop()
        async with self._slots_lock:
            now = loop.time()
            slot = max(now, self._next_slot.get(host, now))
            self._next_slot[host] = slot + self._per_host_interval
        await asyncio.sleep(slot - now)


async def _read_capped(response: httpx.Response) -> str:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_PAGE_BYTES:
            raise FetchError("page is too large")
    return body.decode(response.encoding or "utf-8", errors="replace")
