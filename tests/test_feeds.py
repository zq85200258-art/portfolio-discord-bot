"""Feed parser tests using hand-written XML documents and no HTTP calls."""

import unittest
from pathlib import Path

from portfolio_bot.feeds import MAX_FEED_BYTES, FeedError, normalize_feed_url, parse_feed

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE = "https://example.org/journal/feed.xml"


class FeedTests(unittest.TestCase):
    def test_rss_fills_missing_titles_and_deduplicates(self) -> None:
        feed = parse_feed((FIXTURES / "feed.xml").read_bytes(), SOURCE)
        self.assertEqual(feed.title, "Portfolio Dev Journal")
        self.assertEqual([article.title for article in feed.articles], [
            "Member Roles & Polls", "Untitled article", "Permalink-only entry",
        ])
        self.assertEqual(feed.articles[0].link, "https://example.org/posts/member-roles")
        self.assertIn("id:roles-1", feed.articles[0].keys)
        self.assertEqual(feed.articles[1].keys, ("url:https://example.org/posts/feed-updates",))

    def test_atom_uses_alternate_link_and_xml_base(self) -> None:
        feed = parse_feed((FIXTURES / "atom.xml").read_bytes(), SOURCE)
        self.assertEqual(feed.title, "Dev Log")
        self.assertEqual(len(feed.articles), 1)
        self.assertEqual(feed.articles[0].link, "https://example.org/dev/posts/release")
        self.assertEqual(feed.articles[0].title, "Release Notes & Updates")

    def test_rss1_items_live_under_rdf_root(self) -> None:
        xml = b'''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
                            xmlns="http://purl.org/rss/1.0/">
                    <channel><title>RDF Feed</title></channel>
                    <item rdf:about="item-1"><title>Article</title><link>/article</link></item>
                  </rdf:RDF>'''
        self.assertEqual(parse_feed(xml, SOURCE).articles[0].keys,
                         ("url:https://example.org/article", "id:item-1"))

    def test_empty_channel_is_a_valid_feed(self) -> None:
        feed = parse_feed(b"<rss><channel/></rss>", SOURCE)
        self.assertEqual(feed.articles, ())
        self.assertEqual(feed.title, "example.org")

    def test_invalid_document_does_not_become_an_empty_feed(self) -> None:
        for xml in (b"<rss", b"<rss/>", b"<html/>", b"x" * (MAX_FEED_BYTES + 1)):
            with self.subTest(prefix=xml[:16]), self.assertRaises(FeedError):
                parse_feed(xml, SOURCE)

    def test_rejects_document_type_even_in_utf16(self) -> None:
        xml = '<!DOCTYPE rss [<!ENTITY example "embedded">]><rss><channel><title>&example;</title></channel></rss>'
        for encoding in ("utf-8", "utf-16"):
            with self.subTest(encoding=encoding), self.assertRaisesRegex(FeedError, "document type"):
                parse_feed(xml.encode(encoding), SOURCE)

    def test_normalizes_subscription_url(self) -> None:
        self.assertEqual(normalize_feed_url(" HTTPS://EXAMPLE.ORG.:443/posts?limit=10#new "),
                         "https://example.org/posts?limit=10")

    def test_rejects_unsupported_subscription_urls(self) -> None:
        urls = ("file:///feed.xml", "http://localhost/feed", "https://127.0.0.1/feed", "http://[::1]/",
                "https://example.org:444/feed", "https://name:password@example.org/feed", "https://example.org/has space")
        for url in urls:
            with self.subTest(url=url), self.assertRaises(FeedError):
                normalize_feed_url(url)
