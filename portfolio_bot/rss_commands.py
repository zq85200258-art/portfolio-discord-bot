"""Manage server feed subscriptions through Discord slash commands."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from .feeds import FeedError
from .rss_service import RSSService

logger = logging.getLogger(__name__)


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
class RSSCommands(app_commands.Group, name="rss", description="Manage this server's RSS feeds."):
    """Keep RSS management scoped to servers and authorized moderators."""

    def __init__(self, service: RSSService) -> None:
        super().__init__()
        self.service = service

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id is None or not interaction.permissions.manage_guild:
            raise app_commands.MissingPermissions(["manage_guild"])
        return True

    @app_commands.command(description="Send new articles from a public feed to a text channel.")
    @app_commands.describe(url="Public RSS or Atom feed URL", channel="Destination text or announcement channel")
    async def subscribe(self, interaction: discord.Interaction, url: str, channel: discord.TextChannel) -> None:
        guild = interaction.guild
        if guild is None or channel.guild.id != guild.id or guild.me is None:
            await interaction.response.send_message("Choose a text channel in this server.", ephemeral=True)
            return
        permissions = channel.permissions_for(guild.me)
        if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
            await interaction.response.send_message(
                "I need View Channel, Send Messages, and Embed Links in the destination.", ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            subscription_id = await self.service.subscribe(guild.id, channel.id, url)
        except FeedError as exc:
            await interaction.edit_original_response(content=str(exc))
            return
        if subscription_id is None:
            message = f"{channel.mention} already receives this feed. Use `/rss list` to find its ID."
        else:
            message = f"Subscription **#{subscription_id}** will post new articles in {channel.mention}."
            logger.info("rss_subscribed", extra={"context": {"guild_id": guild.id, "channel_id": channel.id,
                                                              "subscription_id": subscription_id}})
        await interaction.edit_original_response(content=message)

    @app_commands.command(description="Remove a subscription by its ID from /rss list.")
    async def unsubscribe(self, interaction: discord.Interaction,
                          subscription_id: app_commands.Range[int, 1, 2**53 - 1]) -> None:
        guild_id = interaction.guild_id
        if guild_id is not None and self.service.store.remove(guild_id, subscription_id):
            message = f"Removed subscription **#{subscription_id}**."
            logger.info("rss_unsubscribed", extra={"context": {"guild_id": guild_id, "subscription_id": subscription_id}})
        else:
            message = "No subscription with that ID exists in this server."
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="list", description="Show this server's subscriptions and their IDs.")
    async def list_subscriptions(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        subscriptions = [] if guild_id is None else self.service.store.list(guild_id)
        if not subscriptions:
            await interaction.response.send_message("No RSS subscriptions. Use `/rss subscribe` to add one.", ephemeral=True)
            return

        pages: list[str] = []
        page = "**RSS subscriptions**\n"
        for item in subscriptions:
            display_url = item.url.replace("<", "%3C").replace(">", "%3E")
            line = f"**#{item.id}** · <#{item.channel_id}> · <{display_url}>\n"
            if len(page) + len(line) > 2000:
                pages.append(page)
                page = ""
            page += line
        pages.append(page)
        await interaction.response.send_message(pages[0], ephemeral=True)
        for page in pages[1:]:
            await interaction.followup.send(page, ephemeral=True)
