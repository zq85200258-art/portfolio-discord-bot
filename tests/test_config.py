"""Configuration validation and structured log rotation tests."""

import io
import json
import logging
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from portfolio_bot.config import Config, ConfigError, LogConfig, load_config
from portfolio_bot.logging_setup import configure_logging

PROJECT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=PROJECT, prefix=".test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "config.json"
        self.data = json.loads((PROJECT / "config.json").read_text(encoding="utf-8"))

    def load(self) -> Config:
        self.path.write_text(json.dumps(self.data), encoding="utf-8")
        return load_config(self.path)

    def test_config_loads_defaults_and_decimal_discord_ids(self) -> None:
        self.data.pop("sync_guild_id", None)
        self.data["welcome"]["channel_id"] = "123456789012345678"
        self.data["self_assignable_role_ids"] = ["678901234567890123", 111, 111]
        config = self.load()
        self.assertIsNone(config.sync_guild_id)
        self.assertEqual(config.welcome.channel_id, 123456789012345678)
        self.assertEqual(config.self_assignable_role_ids, {678901234567890123, 111})
        self.assertEqual(config.rss.database_path, self.root / "data/subscriptions.sqlite3")

    def test_config_rejects_invalid_template_field(self) -> None:
        self.data["welcome"]["message"] = "Hello {member.password}"
        with self.assertRaisesRegex(ConfigError, "welcome.message"):
            self.load()

    def test_config_rejects_boolean_ids_and_poll_interval(self) -> None:
        self.data["sync_guild_id"] = True
        with self.assertRaisesRegex(ConfigError, "sync_guild_id"):
            self.load()
        self.data["sync_guild_id"] = None
        self.data["rss"]["interval_seconds"] = 0
        with self.assertRaisesRegex(ConfigError, "rss.interval_seconds"):
            self.load()

    def test_paths_are_project_relative_and_distinct(self) -> None:
        self.data["logging"]["file_path"] = "../portfolio-bot.log"
        with self.assertRaisesRegex(ConfigError, "inside the project"):
            self.load()
        self.data["logging"]["file_path"] = self.data["rss"]["database_path"]
        with self.assertRaisesRegex(ConfigError, "must be different"):
            self.load()

    def test_logging_outputs_json_and_rotates(self) -> None:
        path = self.root / "logs" / "bot.log"
        with patch("logging.basicConfig") as basic_config:
            configure_logging(LogConfig("INFO", path, 1024, 1))
        console, file_handler = basic_config.call_args.kwargs["handlers"]
        self.addCleanup(console.close)
        self.addCleanup(file_handler.close)
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "rss_article_delivered", (), None)
        record.context = {"subscription_id": 42, "label": "a" * 600}
        file_handler.handle(record)
        file_handler.handle(record)
        self.assertTrue(path.with_name("bot.log.1").is_file())
        file_event = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(file_event, json.loads(console.format(record)))
        self.assertEqual(file_event["event"], "rss_article_delivered")
        self.assertEqual(file_event["subscription_id"], 42)

    def test_debug_keeps_app_logs_and_blocks_sensitive_library_records(self) -> None:
        log_path = self.root / "bot.log"
        console = io.StringIO()
        sentinel = "fake-interaction-token-for-test"
        root = logging.getLogger()
        previous_level = root.level
        with patch.object(root, "handlers", []), redirect_stderr(console):
            try:
                configure_logging(LogConfig("DEBUG", log_path, 10000, 1))
                logging.getLogger("portfolio_bot.bot").debug("app_debug_message")
                for name in ("discord", "discord.gateway", "discord.http", "discord.client",
                             "discord.webhook.async_", "aiohttp.client"):
                    library = logging.getLogger(name)
                    library.debug("Raw interaction event: %s", {"token": sentinel})
                    library.warning("Retrying https://example.org/interactions/1/%s/callback", sentinel)
            finally:
                for handler in root.handlers:
                    handler.close()
                root.setLevel(previous_level)

        for output in (console.getvalue(), log_path.read_text(encoding="utf-8")):
            self.assertIn("app_debug_message", output)
            self.assertNotIn(sentinel, output)
