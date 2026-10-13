import httpx
import pytest
import respx

from shelfwatch.fetch import MAX_PAGE_BYTES, Fetcher, FetchError, UnsafeURLError, ensure_public_host


async def allow_all(url: httpx.URL) -> None:
    return None


async def deny_internal(url: httpx.URL) -> None:
    if url.host == "internal.test":
        raise UnsafeURLError(url.host)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost:8080/admin",
        "http://169.254.169.254/",
        "ftp://x.test/",
    ],
)
async def test_private_and_odd_urls_are_refused(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        await ensure_public_host(httpx.URL(url))


@respx.mock
async def test_redirect_into_private_network_is_refused() -> None:
    respx.get("https://shop.test/go").respond(302, headers={"Location": "http://internal.test/"})
    async with httpx.AsyncClient() as client:
        fetcher = Fetcher(client, per_host_interval=0, check_url=deny_internal)
        with pytest.raises(UnsafeURLError):
            await fetcher.page("https://shop.test/go")


@respx.mock
async def test_follows_relative_redirect() -> None:
    respx.get("https://shop.test/old").respond(301, headers={"Location": "/new"})
    respx.get("https://shop.test/new").respond(200, text="<title>New</title>")
    async with httpx.AsyncClient() as client:
        fetcher = Fetcher(client, per_host_interval=0, check_url=allow_all)
        assert (await fetcher.product("https://shop.test/old")).title == "New"


@respx.mock
async def test_http_errors_and_huge_pages_become_fetch_errors() -> None:
    respx.get("https://shop.test/403").respond(403)
    respx.get("https://shop.test/huge").respond(200, content=b"x" * (MAX_PAGE_BYTES + 1))
    async with httpx.AsyncClient() as client:
        fetcher = Fetcher(client, per_host_interval=0, check_url=allow_all)
        for path in ("403", "huge"):
            with pytest.raises(FetchError):
                await fetcher.page(f"https://shop.test/{path}")
