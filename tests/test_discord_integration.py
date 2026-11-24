"""Use a real discord.py command tree with mocked Discord interactions and I/O."""

import asyncio
import os
import socket
import tempfile
import unittest
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, PropertyMock, patch

try:
    import discord
except ModuleNotFoundError as exc:
    if exc.name != "discord":
        raise
    raise unittest.SkipTest("Install requirements.txt to run the mocked Discord integration suite.") from exc

from portfolio_bot.bot import PortfolioBot
from portfolio_bot.community import RoleAction, RoleSelect
from portfolio_bot.config import load_config
from portfolio_bot.feeds import Article, Feed, FeedError, parse_feed
from portfolio_bot.proxy import ProxyConfig, ProxyConfigError, load_proxy
from portfolio_bot.rss_http import FeedHTTPClient, PublicResolver

PROJECT = Path(__file__).resolve().parents[1]
FEED_URL = "https://example.org/feed.xml"
PROXY_URL = "http://127.0.0.1:7897"


class ProxyConfigTests(unittest.TestCase):
    def test_reads_nonblank_proxy_values_in_order(self) -> None:
        standard = "https://proxy.example.org:8443"
        for env, expected in (
            ({}, None),
            ({"PROXY_URL": "", "HTTPS_PROXY": ""}, None),
            ({"PROXY_URL": " \t", "HTTPS_PROXY": " \n"}, None),
            ({"PROXY_URL": f"  {PROXY_URL}  "}, PROXY_URL),
            ({"HTTPS_PROXY": standard}, standard),
            ({"PROXY_URL": " \t", "HTTPS_PROXY": standard}, standard),
            ({"PROXY_URL": PROXY_URL, "HTTPS_PROXY": standard}, PROXY_URL),
        ):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                self.assertEqual(load_proxy(), ProxyConfig(expected) if expected else None)

    def test_proxy_credentials_are_separate_from_the_endpoint(self) -> None:
        with patch.dict(os.environ, {"PROXY_URL": "http://test:placeholder@127.0.0.1:7897"}, clear=True):
            proxy = load_proxy()
        self.assertEqual(proxy.url, PROXY_URL)
        self.assertEqual(proxy.auth.login, "test")
        self.assertEqual(proxy.auth.password, "placeholder")
        self.assertNotIn("placeholder", repr(proxy))

    def test_invalid_proxy_fails_without_echoing_the_value(self) -> None:
        for value in ("127.0.0.1:7897", "socks5://127.0.0.1:7897", "https:///", "http://localhost:0",
                      "http://test:placeholder@localhost:bad", "http://localhost:65536"):
            with self.subTest(value=value), patch.dict(os.environ, {"PROXY_URL": value}, clear=True):
                with self.assertRaisesRegex(ProxyConfigError, "Invalid PROXY_URL") as error:
                    load_proxy()
                self.assertNotIn(value, str(error.exception))
                self.assertNotIn("placeholder", str(error.exception))


class DiscordIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=PROJECT, prefix=".test-")
        self.addCleanup(temporary.cleanup)
        config = load_config(PROJECT / "config.json")
        config = replace(config, sync_guild_id=10, self_assignable_role_ids=frozenset({40}),
                         welcome=replace(config.welcome, channel_id=20),
                         rss=replace(config.rss, database_path=Path(temporary.name) / "bot.sqlite3"))
        self.bot = PortfolioBot(config)
        self.addAsyncCleanup(self.bot.close)

        self.role = Mock(spec=discord.Role, id=40)
        self.role.name = "Developer"
        self.role.is_assignable.return_value = True
        self.guild = Mock(spec=discord.Guild, id=10, roles=[self.role])
        self.guild.name = "Dev Guild"
        self.guild.get_role.side_effect = lambda role_id: self.role if role_id == 40 else None
        self.guild.me = SimpleNamespace(guild_permissions=discord.Permissions(manage_roles=True))
        self.channel = Mock(spec=discord.TextChannel, id=20, guild=self.guild, mention="<#20>")
        self.channel.send = AsyncMock()
        self.channel.permissions_for.return_value = discord.Permissions(
            view_channel=True, send_messages=True, embed_links=True,
        )
        self.guild.get_channel.return_value = self.channel
        self.member = Mock(spec=discord.Member, guild=self.guild, bot=False, display_name="Jules", mention="<@50>")
        self.member.display_avatar = SimpleNamespace(url="https://example.org/avatar.png")
        self.member.add_roles = AsyncMock()
        self.member.remove_roles = AsyncMock()
        self.interaction = SimpleNamespace(
            guild=self.guild, guild_id=10, user=self.member, channel_id=20, command=None,
            permissions=discord.Permissions(manage_guild=True),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), is_done=Mock(return_value=False)),
            followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(),
        )

    async def test_registers_command_tree_and_rss_subcommands(self) -> None:
        commands = {command.name: command for command in self.bot.tree.get_commands()}
        self.assertEqual(set(commands), {"help", "ping", "poll", "role", "rss"})
        payload = commands["rss"].to_dict(self.bot.tree)
        self.assertEqual({entry["name"] for entry in payload["options"]}, {"subscribe", "unsubscribe", "list"})
        self.assertEqual(payload["default_member_permissions"], discord.Permissions(manage_guild=True).value)
        self.assertTrue(self.bot.intents.members)
        self.assertFalse(self.bot.intents.message_content)
        await commands["ping"].callback(self.interaction)
        self.assertIn("Gateway", self.interaction.response.send_message.await_args.args[0])
        await commands["help"].callback(self.interaction)
        self.assertIn("/rss subscribe", self.interaction.response.send_message.await_args.kwargs["embed"].description)

    async def test_syncs_to_configured_guild_and_starts_one_worker(self) -> None:
        with (patch.object(self.bot.tree, "sync", new_callable=AsyncMock, return_value=[]) as sync,
              patch("portfolio_bot.bot.FeedHTTPClient") as http,
              patch.object(self.bot.poll_feeds, "start") as start):
            http.return_value.close = AsyncMock()
            await self.bot.setup_hook()
        self.assertEqual(sync.await_args.kwargs["guild"].id, 10)
        self.assertEqual(len(self.bot.tree.get_commands(guild=discord.Object(id=10))), 5)
        start.assert_called_once_with()

    async def test_discord_and_rss_receive_the_same_configured_proxy(self) -> None:
        for proxy in (None, ProxyConfig(PROXY_URL)):
            with self.subTest(proxy=proxy):
                with patch.object(discord.Client, "__init__", autospec=True, side_effect=discord.Client.__init__) as init:
                    bot = PortfolioBot(self.bot.config, proxy=proxy)
                self.addAsyncCleanup(bot.close)
                if proxy is None:
                    self.assertNotIn("proxy", init.call_args.kwargs)
                else:
                    self.assertEqual(init.call_args.kwargs["proxy"], PROXY_URL)
                    self.assertEqual(bot.http.proxy, PROXY_URL)
                with (patch.object(bot.tree, "sync", new_callable=AsyncMock, return_value=[]),
                      patch("portfolio_bot.bot.FeedHTTPClient") as http,
                      patch.object(bot.poll_feeds, "start")):
                    http.return_value.close = AsyncMock()
                    await bot.setup_hook()
                http.assert_called_once_with(proxy=proxy)

    async def test_welcome_sends_embed_and_skips_bot_members(self) -> None:
        await self.bot.on_member_join(self.member)
        embed = self.channel.send.await_args.kwargs["embed"]
        self.assertEqual(embed.title, "Welcome to Dev Guild!")
        self.assertIn("<@50>", embed.description)
        self.member.bot = True
        await self.bot.on_member_join(self.member)
        self.channel.send.assert_awaited_once()

    async def test_slash_role_and_menu_support_claim_and_remove(self) -> None:
        command = self.bot.tree.get_command("role")
        await command.callback(self.interaction, self.role, RoleAction.Claim)
        self.member.add_roles.assert_awaited_once_with(self.role, reason="Self-service role claim")
        await command.callback(self.interaction)
        response = self.interaction.response.send_message.await_args.kwargs
        self.assertTrue(response["ephemeral"])
        claim, remove = response["view"].children
        self.assertEqual(claim.options[0].value, "40")
        self.assertEqual(remove.options[0].label, "Developer")
        with patch.object(RoleSelect, "values", new_callable=PropertyMock, return_value=["40"]):
            await remove.callback(self.interaction)
        self.member.remove_roles.assert_awaited_once_with(self.role, reason="Self-service role removal")

    async def test_roles_reject_unlisted_or_unmanageable_targets(self) -> None:
        command = self.bot.tree.get_command("role")
        await command.callback(self.interaction, Mock(id=41), RoleAction.Claim)
        self.assertIn("self-assignable", self.interaction.response.send_message.await_args.args[0])
        self.role.is_assignable.return_value = False
        await command.callback(self.interaction, self.role, RoleAction.Remove)
        self.assertIn("role position", self.interaction.response.send_message.await_args.args[0])
        self.member.add_roles.assert_not_awaited()
        self.member.remove_roles.assert_not_awaited()

    async def test_poll_seeds_number_reactions_and_validates_choices(self) -> None:
        self.interaction.app_permissions = discord.Permissions(
            add_reactions=True, read_message_history=True, embed_links=True,
        )
        message = SimpleNamespace(add_reaction=AsyncMock())
        self.interaction.original_response = AsyncMock(return_value=message)
        command = self.bot.tree.get_command("poll")
        await command.callback(self.interaction, "When should we meet?", "Friday | Saturday")
        embed = self.interaction.response.send_message.await_args.kwargs["embed"]
        self.assertIn("Friday", embed.description)
        self.assertEqual([call.args[0] for call in message.add_reaction.await_args_list], ["1️⃣", "2️⃣"])
        await command.callback(self.interaction, "When?", "Friday | friday")
        self.assertTrue(self.interaction.response.send_message.await_args.kwargs["ephemeral"])
        self.assertEqual(message.add_reaction.await_count, 2)

    async def test_rss_subscription_commands_and_delivery_use_the_same_store(self) -> None:
        xml = (Path(__file__).parent / "fixtures" / "feed.xml").read_bytes()
        self.bot.rss.fetch = AsyncMock(return_value=parse_feed(xml, FEED_URL))
        group = self.bot.tree.get_command("rss")
        self.assertTrue(await group.interaction_check(self.interaction))
        await group.get_command("subscribe").callback(group, self.interaction, FEED_URL, self.channel)
        self.assertIn("#1", self.interaction.edit_original_response.await_args.kwargs["content"])
        await group.get_command("list").callback(group, self.interaction)
        self.assertIn(FEED_URL, self.interaction.response.send_message.await_args.args[0])

        article = Article("New release", "https://example.org/release")
        self.bot.rss.fetch.return_value = Feed("Dev Journal", (article,))
        with patch.object(self.bot, "get_guild", return_value=self.guild):
            await self.bot.rss.poll_once()
        embed = self.channel.send.await_args.kwargs["embed"]
        self.assertEqual((embed.title, embed.url), (article.title, article.link))
        await group.get_command("unsubscribe").callback(group, self.interaction, 1)
        self.assertEqual(self.bot.store.list(10), [])

    async def test_bad_redirect_does_not_stop_scheduled_polls_or_healthy_delivery(self) -> None:
        bad_url = "https://example.net/bad.xml"
        self.bot.store.add(10, 20, bad_url, ())
        self.bot.store.add(10, 20, FEED_URL, ())
        with patch("portfolio_bot.rss_http.aiohttp.ClientSession"):
            self.bot.feed_http = FeedHTTPClient()
        session = self.bot.feed_http.session
        session.close = AsyncMock()

        bad = MagicMock()
        bad.__aenter__.return_value = SimpleNamespace(
            status=302, headers={"Location": "https://[invalid"}, url=bad_url,
        )

        def update(number: int) -> MagicMock:
            async def chunks(size: int) -> AsyncIterator[bytes]:
                yield (f"<rss><channel><item><title>Update {number}</title>"
                       f"<link>https://example.org/{number}</link></item></channel></rss>").encode()

            response = MagicMock()
            response.__aenter__.return_value = SimpleNamespace(
                status=200, url=FEED_URL, content=SimpleNamespace(iter_chunked=chunks),
            )
            return response

        async def send(*, embed: discord.Embed) -> None:
            if embed.title == "Update 2":
                self.bot.poll_feeds.stop()

        session.get.side_effect = [bad, update(1), bad, update(2)]
        self.channel.send.side_effect = send
        self.bot.poll_feeds.change_interval(seconds=0)
        with (patch.object(self.bot, "get_guild", return_value=self.guild),
              patch.object(self.bot, "wait_until_ready", new_callable=AsyncMock),
              self.assertLogs("portfolio_bot.rss_service", level="WARNING") as logs):
            await asyncio.wait_for(self.bot.poll_feeds.start(), timeout=5)

        self.assertEqual([call.args[0] for call in session.get.call_args_list], [bad_url, FEED_URL] * 2)
        self.assertEqual([call.kwargs["embed"].title for call in self.channel.send.await_args_list],
                         ["Update 1", "Update 2"])
        self.assertEqual([record.getMessage() for record in logs.records], ["rss_fetch_failed"] * 2)

    async def test_rss_skips_invalid_embed_and_retries_other_discord_failures(self) -> None:
        subscription_id = self.bot.store.add(10, 20, FEED_URL, ())
        invalid = Article("Invalid embed", "https://example.org/invalid")
        valid = Article("First update", "https://example.org/first")
        later = Article("Second update", "https://example.org/second")
        self.bot.rss.fetch = AsyncMock(return_value=Feed("Updates", (valid, invalid)))
        self.channel.send.side_effect = [
            discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"), {"code": 50035}),
            None,
            discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"), {"code": 50008}),
            discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), {"code": 50013}),
            discord.HTTPException(SimpleNamespace(status=503, reason="Service Unavailable"), None),
            None,
        ]
        with (patch.object(self.bot, "get_guild", return_value=self.guild),
              self.assertLogs("portfolio_bot.rss_service", level="WARNING") as logs):
            await self.bot.rss.poll_once()
            self.assertEqual(self.bot.store.seen_keys(subscription_id), set(invalid.keys + valid.keys))
            self.bot.rss.fetch.return_value = Feed("Updates", (later, valid, invalid))
            for _ in range(3):
                await self.bot.rss.poll_once()
                self.assertFalse(self.bot.store.seen_keys(subscription_id).intersection(later.keys))
            await self.bot.rss.poll_once()

        self.assertEqual([call.kwargs["embed"].title for call in self.channel.send.await_args_list],
                         [invalid.title, valid.title, *([later.title] * 4)])
        self.assertEqual([record.getMessage() for record in logs.records],
                         ["rss_article_rejected", *(["rss_delivery_failed"] * 3)])
        self.assertTrue(set(later.keys).issubset(self.bot.store.seen_keys(subscription_id)))

    async def test_rss_permission_check_rejects_regular_members(self) -> None:
        self.interaction.permissions = discord.Permissions.none()
        group = self.bot.tree.get_command("rss")
        with self.assertRaises(discord.app_commands.MissingPermissions) as error:
            await group.interaction_check(self.interaction)
        with self.assertLogs("portfolio_bot.bot", level="WARNING"):
            await self.bot.on_command_error(self.interaction, error.exception)
        self.assertIn("Manage Server", self.interaction.response.send_message.await_args.args[0])


class FeedHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_aiohttp_routes_feed_request_to_loopback_proxy(self) -> None:
        client = FeedHTTPClient(proxy=ProxyConfig(PROXY_URL))
        self.addAsyncCleanup(client.close)
        connector = client.session.connector
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with (patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=addresses)) as dns,
              patch.object(connector, "_wrap_create_connection", side_effect=OSError("mocked dial")) as dial,
              patch.object(connector, "_create_proxy_connection", wraps=connector._create_proxy_connection) as proxied):
            with self.assertRaisesRegex(FeedError, "Could not retrieve"):
                await client.fetch(FEED_URL)
        dns.assert_awaited_once_with("example.org", 443, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
        proxied.assert_awaited_once()
        request = proxied.await_args.args[0]
        self.assertEqual(str(request.url), FEED_URL)
        self.assertEqual(str(request.proxy), PROXY_URL)
        dial.assert_awaited_once()
        self.assertEqual([address[4] for address in dial.await_args.kwargs["addr_infos"]], [("127.0.0.1", 7897)])

    async def test_proxy_rechecks_redirect_destination_dns(self) -> None:
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))
        with patch("portfolio_bot.rss_http.aiohttp.ClientSession") as session:
            client = FeedHTTPClient(proxy=ProxyConfig(PROXY_URL))
        response = session.return_value.get.return_value
        response.__aenter__.return_value = SimpleNamespace(
            status=302, headers={"Location": "https://internal.example.org/feed.xml"}, url=FEED_URL,
        )
        dns = AsyncMock(side_effect=[[public], [public, private]])
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", dns):
            with self.assertRaises(FeedError):
                await client.fetch(FEED_URL)
        self.assertEqual([call.args[0] for call in dns.await_args_list], ["example.org", "internal.example.org"])
        session.return_value.get.assert_called_once_with(FEED_URL, allow_redirects=False, proxy=PROXY_URL, proxy_auth=None)

    async def test_http_parses_fixture_after_redirect(self) -> None:
        redirect = "https://example.org/latest.xml"
        payload = (Path(__file__).parent / "fixtures" / "feed.xml").read_bytes()

        async def chunks(size: int) -> AsyncIterator[bytes]:
            self.assertGreater(size, 0)
            yield payload

        with patch("portfolio_bot.rss_http.aiohttp.ClientSession") as session:
            client = FeedHTTPClient()
        client.session.close = AsyncMock()
        self.addAsyncCleanup(client.close)
        first = MagicMock()
        first.__aenter__.return_value = SimpleNamespace(status=302, headers={"Location": "/latest.xml"}, url=FEED_URL)
        second = MagicMock()
        second.__aenter__.return_value = SimpleNamespace(
            status=200, url=redirect, content=SimpleNamespace(iter_chunked=chunks),
        )
        session.return_value.get.side_effect = [first, second]
        feed = await client.fetch(FEED_URL)
        self.assertEqual(feed.title, "Portfolio Dev Journal")
        self.assertEqual([call.args[0] for call in client.session.get.call_args_list], [FEED_URL, redirect])

    async def test_http_rejects_nonpublic_redirect_before_second_request(self) -> None:
        with patch("portfolio_bot.rss_http.aiohttp.ClientSession") as session:
            client = FeedHTTPClient()
        client.session.close = AsyncMock()
        self.addAsyncCleanup(client.close)
        response = MagicMock()
        response.__aenter__.return_value = SimpleNamespace(
            status=302, headers={"Location": "http://127.0.0.1/feed.xml"}, url=FEED_URL,
        )
        session.return_value.get.return_value = response
        with self.assertRaises(FeedError):
            await client.fetch(FEED_URL)
        session.return_value.get.assert_called_once_with(FEED_URL, allow_redirects=False)

    async def test_http_wraps_unexpected_feed_parse_errors(self) -> None:
        with patch("portfolio_bot.rss_http.aiohttp.ClientSession") as session:
            client = FeedHTTPClient()
        client.session.close = AsyncMock()
        self.addAsyncCleanup(client.close)

        async def chunks(size: int) -> AsyncIterator[bytes]:
            yield b"<rss/>"

        session.return_value.get.return_value.__aenter__.return_value = SimpleNamespace(
            status=200, url=FEED_URL, content=SimpleNamespace(iter_chunked=chunks),
        )
        with patch("portfolio_bot.rss_http.parse_feed", side_effect=ValueError("Invalid parsed value")):
            with self.assertRaisesRegex(FeedError, "feed URL or content") as error:
                await client.fetch(FEED_URL)
        self.assertIsInstance(error.exception.__cause__, ValueError)

    async def test_resolver_filters_nonpublic_results(self) -> None:
        results = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 443))
                   for host in ("127.0.0.1", "10.0.0.1", "93.184.216.34")]
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=results)) as dns:
            resolved = await PublicResolver().resolve("example.org", 443)
            self.assertEqual([record["host"] for record in resolved], ["93.184.216.34"])
            dns.return_value = results[:2]
            with self.assertRaisesRegex(OSError, "no public addresses"):
                await PublicResolver().resolve("example.org", 443)
