"""Wire Discord events and slash commands to the feed worker."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import suppress

import discord
from discord import app_commands
from discord.ext import tasks

from .community import register_community, welcome_member
from .config import Config
from .feeds import Article, Feed
from .proxy import ProxyConfig
from .rss_commands import RSSCommands
from .rss_http import FeedHTTPClient
from .rss_service import ArticleRejected, DeliveryError, RSSService
from .subscriptions import Subscription, SubscriptionStore

logger = logging.getLogger(__name__)


class PortfolioBot(discord.Client):
    """A slash-only Discord client with a single managed polling task."""

    def __init__(self, config: Config, *, proxy: ProxyConfig | None = None) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = config.welcome.channel_id is not None
        proxy_options = {"proxy": proxy.url, "proxy_auth": proxy.auth} if proxy else {}
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none(), **proxy_options)
        self.proxy = proxy
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.tree.error(self.on_command_error)
        self.store = SubscriptionStore(config.rss.database_path)
        self.feed_http: FeedHTTPClient | None = None
        self.rss = RSSService(self.store, self.fetch_feed, self.deliver_article)
        register_community(self.tree, self, config)
        self.tree.add_command(RSSCommands(self.rss))

    async def setup_hook(self) -> None:
        """Register the command tree and start polling once the Gateway is ready."""
        guild_id = self.config.sync_guild_id
        if guild_id is None:
            synced = await self.tree.sync()
        else:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        logger.info("commands_synced", extra={"context": {"count": len(synced), "guild_id": guild_id}})
        self.feed_http = FeedHTTPClient(proxy=self.proxy)
        self.poll_feeds.change_interval(seconds=self.config.rss.interval_seconds)
        self.poll_feeds.start()

    async def on_ready(self) -> None:
        logger.info("discord_ready", extra={"context": {"bot_id": self.user.id if self.user else None,
                                                          "guild_count": len(self.guilds)}})

    async def on_member_join(self, member: discord.Member) -> None:
        await welcome_member(member, self.config.welcome)

    async def on_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        """Respond privately and keep Discord response payloads out of logs."""
        original = getattr(error, "original", error)
        logger.warning("command_failed", extra={"context": {
            "guild_id": interaction.guild_id, "error_type": type(original).__name__,
            "command": interaction.command.qualified_name if interaction.command else None,
            "status": getattr(original, "status", None),
        }})
        if isinstance(error, app_commands.MissingPermissions) and "manage_guild" in error.missing_permissions:
            message = "You need Manage Server to manage RSS subscriptions."
        else:
            message = "The command could not finish. Check its options and the bot's permissions, then try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            logger.warning("command_error_reply_failed")

    async def fetch_feed(self, url: str) -> Feed:
        if self.feed_http is None:
            raise RuntimeError("RSS HTTP client has not started")
        return await self.feed_http.fetch(url)

    async def deliver_article(self, subscription: Subscription, feed: Feed, article: Article) -> None:
        """Send an article embed and distinguish validation failures from delivery failures."""
        guild = self.get_guild(subscription.guild_id)
        channel = guild.get_channel(subscription.channel_id) if guild else None
        if not isinstance(channel, discord.TextChannel):
            raise DeliveryError("Destination text channel is unavailable.")
        embed = discord.Embed(title=article.title[:256], url=article.link, colour=discord.Colour.blurple())
        embed.set_footer(text=feed.title[:2048])
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            if exc.status == 400 and exc.code == 50035:  # Invalid Form Body
                raise ArticleRejected("Discord rejected the article embed.") from exc
            raise DeliveryError(f"Discord returned HTTP {exc.status}.") from exc

    @tasks.loop(seconds=300)
    async def poll_feeds(self) -> None:
        await self.wait_until_ready()
        try:
            await self.rss.poll_once()
        except sqlite3.Error as exc:
            logger.error("rss_storage_failed", extra={"context": {"error_type": type(exc).__name__}})

    async def close(self) -> None:
        """Stop the worker and release the feed connector before Discord closes."""
        try:
            task = self.poll_feeds.get_task()
            if task is not None and not task.done():
                self.poll_feeds.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if self.feed_http is not None:
                await self.feed_http.close()
        finally:
            await super().close()
