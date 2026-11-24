"""Exercise fixture-based subscriptions and retry behavior with mocked I/O."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from portfolio_bot.feeds import Article, Feed, FeedError, parse_feed
from portfolio_bot.rss_service import DeliveryError, RSSService
from portfolio_bot.subscriptions import SubscriptionStore

PROJECT = Path(__file__).resolve().parents[1]
URL = "https://example.org/journal/feed.xml"
NEXT = Article("Release notes", "https://example.org/release", "release-1")
NEWEST = Article("Bug fixes", "https://example.org/fixes", "fixes-1")


class RSSServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(dir=PROJECT, prefix=".test-")
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "rss.sqlite3"
        self.store = SubscriptionStore(self.db)
        xml = (Path(__file__).parent / "fixtures" / "feed.xml").read_bytes()
        self.initial = parse_feed(xml, URL)
        self.fetch = AsyncMock(return_value=self.initial)
        self.deliver = AsyncMock()
        self.service = RSSService(self.store, self.fetch, self.deliver)

    async def test_baseline_dedup_delivery_order_and_restart(self) -> None:
        subscription_id = await self.service.subscribe(100, 200, URL)
        self.assertIsNotNone(subscription_id)
        self.assertIsNone(await self.service.subscribe(100, 200, URL))
        await self.service.poll_once()
        self.deliver.assert_not_awaited()

        self.fetch.return_value = Feed(self.initial.title, (NEWEST, NEXT, *self.initial.articles))
        await self.service.poll_once()
        self.assertEqual([call.args[2] for call in self.deliver.await_args_list], [NEXT, NEWEST])
        self.deliver.reset_mock()

        changed_link = Article(NEXT.title, f"{NEXT.link}/updated", NEXT.identifier)
        changed_id = Article(NEWEST.title, NEWEST.link, "reissued-id")
        self.fetch.return_value = Feed(self.initial.title, (changed_link, changed_id))
        restarted = RSSService(SubscriptionStore(self.db), self.fetch, self.deliver)
        await restarted.poll_once()
        self.deliver.assert_not_awaited()

    async def test_scoped_unsubscribe_and_independent_destinations(self) -> None:
        first = await self.service.subscribe(100, 200, URL)
        second = await self.service.subscribe(101, 201, URL)
        self.assertEqual([sub.id for sub in self.store.list(100)], [first])
        self.assertFalse(self.store.remove(100, second))
        self.fetch.reset_mock()
        self.fetch.return_value = Feed("Updates", (NEXT,))
        await self.service.poll_once()
        self.fetch.assert_awaited_once_with(URL)
        self.assertEqual([call.args[0].id for call in self.deliver.await_args_list], [first, second])
        self.assertTrue(self.store.remove(100, first))
        self.assertFalse(self.store.seen_keys(first))
        recreated = await self.service.subscribe(100, 200, URL)
        self.assertNotEqual(recreated, first)

    async def test_delivery_failure_is_retried_without_reposting_successes(self) -> None:
        await self.service.subscribe(100, 200, URL)
        self.fetch.return_value = Feed("Updates", (NEWEST, NEXT))
        self.deliver.side_effect = [None, DeliveryError("Temporarily unavailable"), None]
        with self.assertLogs("portfolio_bot.rss_service", level="WARNING"):
            await self.service.poll_once()
        await self.service.poll_once()
        self.assertEqual([call.args[2] for call in self.deliver.await_args_list], [NEXT, NEWEST, NEWEST])

    async def test_bad_feed_does_not_stop_other_subscriptions(self) -> None:
        await self.service.subscribe(100, 200, URL)
        await self.service.subscribe(100, 201, "https://example.net/feed")
        self.fetch.side_effect = [FeedError("Invalid XML"), Feed("Updates", (NEXT,))]
        with self.assertLogs("portfolio_bot.rss_service", level="WARNING"):
            await self.service.poll_once()
        self.deliver.assert_awaited_once()
        self.assertEqual(self.deliver.await_args.args[0].channel_id, 201)

    async def test_failed_validation_leaves_database_untouched(self) -> None:
        self.fetch.side_effect = FeedError("Invalid XML")
        with self.assertRaises(FeedError):
            await self.service.subscribe(100, 200, URL)
        self.assertEqual(self.store.list(), [])

    async def test_unsubscribe_while_fetch_is_in_progress(self) -> None:
        subscription_id = await self.service.subscribe(100, 200, URL)

        async def unsubscribe_during_fetch(url: str) -> Feed:
            self.store.remove(100, subscription_id)
            return Feed("Updates", (NEXT,))

        self.fetch.side_effect = unsubscribe_during_fetch
        await self.service.poll_once()
        self.deliver.assert_not_awaited()
