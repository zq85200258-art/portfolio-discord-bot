"""Subscribe to feeds and coordinate polling without a Discord dependency."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from .feeds import Article, Feed, FeedError, normalize_feed_url
from .subscriptions import Subscription, SubscriptionStore

logger = logging.getLogger(__name__)

FetchFeed = Callable[[str], Awaitable[Feed]]
DeliverArticle = Callable[[Subscription, Feed, Article], Awaitable[None]]


class DeliveryError(RuntimeError):
    """A destination rejected an article and should be retried later."""


class ArticleRejected(DeliveryError):
    """The destination permanently rejected this article's content."""


class RSSService:
    """Prime new subscriptions with the feed's current entries and deliver updates."""

    def __init__(self, store: SubscriptionStore, fetch: FetchFeed, deliver: DeliverArticle) -> None:
        self.store = store
        self.fetch = fetch
        self.deliver = deliver

    async def subscribe(self, guild_id: int, channel_id: int, url: str) -> int | None:
        """Validate a live feed and start watching for articles published after setup."""
        url = normalize_feed_url(url)
        feed = await self.fetch(url)
        keys = (key for article in feed.articles for key in article.keys)
        return self.store.add(guild_id, channel_id, url, keys)

    async def poll_once(self) -> None:
        """Fetch each distinct feed once and retry failed deliveries next cycle."""
        feeds: dict[str, Feed | None] = {}
        for subscription in self.store.list():
            context = {"subscription_id": subscription.id, "channel_id": subscription.channel_id}
            if subscription.url not in feeds:
                try:
                    feeds[subscription.url] = await self.fetch(subscription.url)
                except FeedError as exc:
                    logger.warning("rss_fetch_failed", extra={"context": {**context, "reason": str(exc)}})
                    feeds[subscription.url] = None
            feed = feeds[subscription.url]
            if feed is None:
                continue

            seen = self.store.seen_keys(subscription.id)
            for article in reversed(feed.articles):
                if seen.intersection(article.keys):
                    continue
                if not self.store.exists(subscription.id):
                    break
                try:
                    await self.deliver(subscription, feed, article)
                except ArticleRejected as exc:
                    self.store.mark_seen(subscription.id, article.keys)
                    seen.update(article.keys)
                    logger.warning("rss_article_rejected", extra={"context": {**context, "reason": str(exc)}})
                    continue
                except DeliveryError as exc:
                    logger.warning("rss_delivery_failed", extra={"context": {**context, "reason": str(exc)}})
                    break
                self.store.mark_seen(subscription.id, article.keys)
                seen.update(article.keys)
                logger.info("rss_article_delivered", extra={"context": context})
