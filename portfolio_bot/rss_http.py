"""Fetch public RSS and Atom documents with bounded asynchronous HTTP requests."""

from __future__ import annotations

import asyncio
import socket
from ipaddress import IPv6Address, ip_address
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

from .feeds import MAX_FEED_BYTES, Feed, FeedError, normalize_feed_url, parse_feed
from .proxy import ProxyConfig

_REDIRECTS = {301, 302, 303, 307, 308}


class PublicResolver(AbstractResolver):
    """Give the HTTP connector only public IP addresses from the DNS lookup."""

    def __init__(self, *, require_all_public: bool = False) -> None:
        self._require_all_public = require_all_public

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[ResolveResult]:
        records = await asyncio.get_running_loop().getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
        results: list[ResolveResult] = []
        for address_family, _, protocol, _, sockaddr in records:
            address = ip_address(sockaddr[0])
            if isinstance(address, IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
            if address.is_global and not address.is_multicast and not address.is_reserved:
                results.append(ResolveResult(hostname=host, host=sockaddr[0], port=port,
                                             family=address_family, proto=protocol, flags=socket.AI_NUMERICHOST))
            elif self._require_all_public:
                raise OSError("Feed hostname resolves to a nonpublic address.")
        if not results:
            raise OSError("Feed hostname has no public addresses.")
        return results

    async def close(self) -> None:
        """The event loop owns the DNS executor."""


class FeedHTTPClient:
    """Reuse one connector across subscriptions and require public hosts at every hop."""

    def __init__(self, *, proxy: ProxyConfig | None = None) -> None:
        self._proxy_options = {"proxy": proxy.url, "proxy_auth": proxy.auth} if proxy else {}
        self._destination_resolver = PublicResolver(require_all_public=True) if proxy else None
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector() if proxy else aiohttp.TCPConnector(resolver=PublicResolver()),
            cookie_jar=aiohttp.DummyCookieJar(),
            headers={
                "User-Agent": "PortfolioDiscordBot/1.0",
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*;q=0.1",
            },
        )

    async def close(self) -> None:
        """Close all feed connections."""
        await self.session.close()

    async def fetch(self, url: str) -> Feed:
        """Follow at most four redirects and parse up to 2 MiB of decoded XML."""
        try:
            async with asyncio.timeout(15):
                for _ in range(5):
                    url = normalize_feed_url(url)
                    if self._destination_resolver is not None:
                        # aiohttp resolves the proxy hop. Check the feed host separately before forwarding it.
                        destination = urlsplit(url)
                        port = 443 if destination.scheme == "https" else 80
                        await self._destination_resolver.resolve(destination.netloc, port, socket.AF_UNSPEC)
                    async with self.session.get(url, allow_redirects=False, **self._proxy_options) as response:
                        if response.status in _REDIRECTS and (location := response.headers.get("Location")):
                            url = urljoin(str(response.url), location)
                            continue
                        if response.status != 200:
                            raise FeedError(f"Feed server returned HTTP {response.status}.")
                        data = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            data.extend(chunk)
                            if len(data) > MAX_FEED_BYTES:
                                raise FeedError("Feed exceeds the 2 MiB response limit.")
                        return parse_feed(bytes(data), str(response.url))
                raise FeedError("Feed exceeded the redirect limit.")
        except FeedError:
            raise
        except TimeoutError as exc:
            raise FeedError("Feed request timed out.") from exc
        except (aiohttp.ClientError, OSError) as exc:
            raise FeedError(f"Could not retrieve the feed ({type(exc).__name__}).") from exc
        except (ValueError, TypeError, LookupError) as exc:
            raise FeedError("Could not interpret the feed URL or content.") from exc
