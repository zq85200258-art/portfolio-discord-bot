"""Parse RSS and Atom and normalize public feed URLs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_FEED_URL = 500
_ATOM = "{http://www.w3.org/2005/Atom}"
_RSS1 = "{http://purl.org/rss/1.0/}"
_RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
_XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)


class FeedError(ValueError):
    """A feed could not be fetched or parsed."""


@dataclass(frozen=True, slots=True)
class Article:
    title: str
    link: str
    identifier: str | None = None

    @property
    def keys(self) -> tuple[str, ...]:
        """Use either a stable publisher ID or an article URL to match duplicates."""
        link_key = f"url:{self.link}"
        return (link_key, f"id:{self.identifier}") if self.identifier else (link_key,)


@dataclass(frozen=True, slots=True)
class Feed:
    title: str
    articles: tuple[Article, ...]


def normalize_feed_url(value: str) -> str:
    """Accept HTTP(S) feed URLs with a DNS hostname and no credentials or fragment."""
    value = value.strip()
    message = "Enter a public HTTP or HTTPS feed URL without credentials or a custom port."
    if not value or len(value) > MAX_FEED_URL or any(ord(char) < 33 or char == "\\" for char in value):
        raise FeedError(message)
    try:
        url = urlsplit(value)
        host = (url.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        port = url.port
    except ValueError as exc:
        raise FeedError(message) from exc
    labels = host.split(".")
    if (
        url.scheme not in {"http", "https"}
        or url.username is not None
        or port not in (None, 443 if url.scheme == "https" else 80)
        or len(host) > 253
        or len(labels) < 2
        or not labels[-1][0:1].isalpha()
        or not all(_HOST_LABEL.fullmatch(label) for label in labels)
    ):
        raise FeedError(message)
    normalized = url._replace(netloc=host, path=url.path or "/", fragment="").geturl()
    if len(normalized) > MAX_FEED_URL:
        raise FeedError(f"Feed URLs must be at most {MAX_FEED_URL} characters.")
    return normalized


class _NoDoctype(ET.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise FeedError("Feed XML contains an unsupported document type.")


class _TitleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _text(node: ET.Element | None) -> str:
    return "" if node is None else "".join(node.itertext()).strip()


def _title(node: ET.Element | None, fallback: str) -> str:
    value = _text(node)
    if node is not None and node.get("type") == "html":
        parser = _TitleText()
        try:
            parser.feed(value)
            parser.close()
        except AssertionError:
            value = ""
        else:
            value = "".join(parser.parts)
    return " ".join(value.split()) or fallback


def _article_url(base: str, value: str) -> str | None:
    if not value or any(ord(char) < 33 or char == "\\" for char in value):
        return None
    try:
        candidate = urlsplit(urljoin(base, value))
        if (candidate.scheme in {"http", "https"} and candidate.hostname and candidate.username is None
                and candidate.port != 0):
            link = candidate._replace(fragment="").geturl()
            if len(link) <= 2048:
                return link
    except ValueError:
        pass
    return None


def _article(entry: ET.Element, namespace: str, base: str) -> Article | None:
    base = _article_url(base, entry.get(_XML_BASE, "")) or base
    if namespace == _ATOM:
        identifier = _text(entry.find(f"{namespace}id"))
        link = None
        for node in entry.findall(f"{namespace}link"):
            if node.get("rel", "alternate") == "alternate":
                link_base = _article_url(base, node.get(_XML_BASE, "")) or base
                link = _article_url(link_base, node.get("href", "").strip())
                if link:
                    break
    else:
        guid = entry.find(f"{namespace}guid")
        identifier = _text(guid) or entry.get(f"{_RDF}about", "")
        node = entry.find(f"{namespace}link")
        link_base = base if node is None else (_article_url(base, node.get(_XML_BASE, "")) or base)
        link = _article_url(link_base, _text(node))
        if not link and guid is not None and guid.get("isPermaLink", "true").lower() == "true":
            link = _article_url(base, identifier) if identifier.startswith(("http://", "https://")) else None
    if not link:
        return None
    return Article(_title(entry.find(f"{namespace}title"), "Untitled article"), link, identifier or None)


def parse_feed(payload: bytes, source_url: str) -> Feed:
    """Read RSS 2.0, RSS 1.0, or Atom, skipping entries with no usable link."""
    if len(payload) > MAX_FEED_BYTES:
        raise FeedError("Feed exceeds the 2 MiB response limit.")
    try:
        root = ET.fromstring(payload, parser=ET.XMLParser(target=_NoDoctype()))
    except FeedError:
        raise
    except (ET.ParseError, LookupError, ValueError) as exc:
        raise FeedError("The response is not well-formed feed XML.") from exc

    base = _article_url(source_url, root.get(_XML_BASE, "")) or source_url
    if root.tag == "rss":
        namespace = ""
        channel = root.find("channel")
        entries = [] if channel is None else channel.findall("item")
    elif root.tag == f"{_RDF}RDF":
        namespace = _RSS1
        channel = root.find(f"{namespace}channel")
        entries = root.findall(f"{namespace}item")
    elif root.tag == f"{_ATOM}feed":
        namespace = _ATOM
        channel = root
        entries = root.findall(f"{namespace}entry")
    else:
        raise FeedError("The response is not an RSS or Atom feed.")
    if channel is None:
        raise FeedError("RSS feed has no channel element.")
    if root.tag == "rss":
        base = _article_url(base, channel.get(_XML_BASE, "")) or base
    title = _title(channel.find(f"{namespace}title"), urlsplit(source_url).hostname or "RSS feed")

    articles: list[Article] = []
    seen: set[str] = set()
    for entry in entries:
        article = _article(entry, namespace, base)
        if article is None or seen.intersection(article.keys):
            continue
        seen.update(article.keys)
        articles.append(article)
    return Feed(title, tuple(articles))
