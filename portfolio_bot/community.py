"""Welcome embeds, role menus, and everyday slash commands."""

from __future__ import annotations

import logging
import math
from enum import Enum

import discord
from discord import app_commands

from .config import Config, WelcomeConfig

logger = logging.getLogger(__name__)
_POLL_EMOJI = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣")
_ACCENT = discord.Colour.blurple()


async def welcome_member(member: discord.Member, config: WelcomeConfig) -> None:
    """Send the configured welcome embed to the new member's server."""
    if config.channel_id is None or member.bot:
        return
    channel = member.guild.get_channel(config.channel_id)
    if not isinstance(channel, discord.TextChannel):
        return
    values = {"server": member.guild.name, "mention": member.mention, "member": member.display_name}
    embed = discord.Embed(title=config.title.format(**values)[:256],
                          description=config.message.format(**values)[:4096], colour=_ACCENT)
    embed.set_thumbnail(url=member.display_avatar.url)
    try:
        await channel.send(embed=embed)
    except discord.HTTPException as exc:
        logger.warning("welcome_failed", extra={"context": {"guild_id": member.guild.id,
                       "channel_id": channel.id, "status": exc.status}})


class RoleAction(str, Enum):
    Claim = "claim"
    Remove = "remove"


def _can_manage_roles(guild: discord.Guild) -> bool:
    bot_member = guild.me
    return bot_member is not None and bot_member.guild_permissions.manage_roles


async def change_role(
    interaction: discord.Interaction, allowed: frozenset[int], role_id: int, action: RoleAction,
) -> None:
    """Apply the same allowlist and hierarchy rules to slash and menu actions."""
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await interaction.response.send_message("Run this command in a server.", ephemeral=True)
        return
    role = guild.get_role(role_id)
    if role_id not in allowed or role is None:
        await interaction.response.send_message("Choose one of this server's self-assignable roles.", ephemeral=True)
        return
    if not _can_manage_roles(guild) or not role.is_assignable():
        await interaction.response.send_message(
            "Ask a moderator to check my Manage Roles permission and role position.", ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        if action is RoleAction.Claim:
            await member.add_roles(role, reason="Self-service role claim")
            message = f"You now have **{discord.utils.escape_markdown(role.name)}**."
        else:
            await member.remove_roles(role, reason="Self-service role removal")
            message = f"Removed **{discord.utils.escape_markdown(role.name)}**."
    except discord.HTTPException as exc:
        logger.warning("role_change_failed", extra={"context": {"guild_id": guild.id, "role_id": role.id,
                                                                 "action": action.value, "status": exc.status}})
        message = "Discord could not update that role. Try again or contact a moderator."
    await interaction.edit_original_response(content=message)


class RoleSelect(discord.ui.Select):
    """Offer one action for the allowed roles visible in this server."""

    def __init__(self, allowed: frozenset[int], roles: list[discord.Role], action: RoleAction) -> None:
        super().__init__(placeholder=f"{action.name} a role", options=[
            discord.SelectOption(label=role.name, value=str(role.id)) for role in roles
        ])
        self.allowed = allowed
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        await change_role(interaction, self.allowed, int(self.values[0]), self.action)


def register_community(tree: app_commands.CommandTree, client: discord.Client, config: Config) -> None:
    """Attach the community command callbacks to the client's command tree."""

    @tree.command(name="ping", description="Show the Discord Gateway latency.")
    async def ping(interaction: discord.Interaction) -> None:
        latency = client.latency
        if math.isfinite(latency):
            message = f"Gateway latency: **{latency * 1000:.0f} ms**"
        else:
            message = "Waiting for the first Gateway heartbeat."
        await interaction.response.send_message(message, ephemeral=True)

    @tree.command(name="help", description="List the bot's slash commands.")
    async def help_command(interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="Bot commands", colour=_ACCENT, description=(
            "`/ping` — Show the Discord Gateway latency.\n"
            "`/help` — Open this guide.\n"
            "`/role` — Open the role menus, or choose a role and action directly.\n"
            "`/poll` — Create a vote with 2–5 options separated by `|`.\n\n"
            "**RSS · Manage Server**\n"
            "`/rss subscribe` — Send new feed articles to a text channel.\n"
            "`/rss unsubscribe` — Remove a subscription by ID.\n"
            "`/rss list` — List this server's subscriptions and IDs."
        ))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="role", description="Claim or remove one of the server's self-assignable roles.")
    @app_commands.guild_only()
    @app_commands.describe(
        role="Pick a role or leave blank to open the menus", action="Claim or remove the selected role",
    )
    async def role_command(interaction: discord.Interaction, role: discord.Role | None = None,
                           action: RoleAction = RoleAction.Claim) -> None:
        allowed = config.self_assignable_role_ids
        if role is not None:
            await change_role(interaction, allowed, role.id, action)
            return

        guild = interaction.guild
        if guild is None or not _can_manage_roles(guild):
            await interaction.response.send_message(
                "Ask a moderator to enable my Manage Roles permission.", ephemeral=True,
            )
            return
        roles = [item for item in guild.roles if item.id in allowed and item.is_assignable()]
        if not roles:
            await interaction.response.send_message(
                "This server has no self-assignable roles configured.", ephemeral=True,
            )
            return
        view = discord.ui.View(timeout=180)
        for choice in RoleAction:
            view.add_item(RoleSelect(allowed, roles, choice))
        await interaction.response.send_message("Choose a role to claim or remove. This menu expires in 3 minutes.",
                                                view=view, ephemeral=True)

    @tree.command(name="poll", description="Create a poll using number reactions.")
    @app_commands.guild_only()
    @app_commands.describe(question="Question to put to a vote", options="Separate 2–5 options with |, e.g. Friday | Saturday")
    async def poll(interaction: discord.Interaction, question: app_commands.Range[str, 1, 256],
                   options: app_commands.Range[str, 1, 520]) -> None:
        labels = [label.strip() for label in options.split("|")]
        if not question.strip() or not 2 <= len(labels) <= 5 or any(not 1 <= len(label) <= 100 for label in labels):
            await interaction.response.send_message(
                "Enter a question and 2–5 options separated by `|`. Each option can contain up to 100 characters.",
                ephemeral=True,
            )
            return
        if len({label.casefold() for label in labels}) != len(labels):
            await interaction.response.send_message("Give each poll option a different label.", ephemeral=True)
            return
        permissions = interaction.app_permissions
        if not (permissions.add_reactions and permissions.read_message_history and permissions.embed_links):
            await interaction.response.send_message("I need Add Reactions, Read Message History, and Embed Links here.",
                                                    ephemeral=True)
            return
        choices = "\n".join(
            f"{emoji} {discord.utils.escape_markdown(label)}" for emoji, label in zip(_POLL_EMOJI, labels)
        )
        embed = discord.Embed(title=question.strip(), description=choices, colour=_ACCENT)
        embed.set_footer(text="Vote for any option by reacting with its number. The bot adds the first reaction.")
        await interaction.response.send_message(embed=embed)
        try:
            message = await interaction.original_response()
            for emoji in _POLL_EMOJI[:len(labels)]:
                await message.add_reaction(emoji)
        except discord.HTTPException as exc:
            logger.warning("poll_reaction_failed", extra={"context": {"channel_id": interaction.channel_id,
                                                                       "status": exc.status}})
            await interaction.followup.send(
                "The poll was posted. Some reactions need to be added manually.", ephemeral=True,
            )
