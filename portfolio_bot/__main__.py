"""Run the bot with `python -m portfolio_bot`."""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

from .bot import PortfolioBot
from .config import ConfigError, load_config
from .logging_setup import configure_logging
from .proxy import ProxyConfigError, load_proxy


def main() -> int:
    """Read configuration and run the client's Gateway connection."""
    root = Path(__file__).resolve().parent.parent
    if not os.environ.get("DISCORD_TOKEN"):
        load_dotenv(root / ".env", interpolate=False)
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        print("Set DISCORD_TOKEN in .env or the process environment.", file=sys.stderr)
        return 2

    try:
        proxy = load_proxy()
        config = load_config(root / "config.json")
        configure_logging(config.logging)
        bot = PortfolioBot(config, proxy=proxy)
    except (ConfigError, ProxyConfigError, OSError, sqlite3.Error) as exc:
        print(f"Startup error: {exc}", file=sys.stderr)
        return 2

    logger = logging.getLogger(__name__)
    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        logger.error("discord_login_failed", extra={"context": {"reason": "Check DISCORD_TOKEN."}})
        return 1
    except discord.PrivilegedIntentsRequired:
        logger.error("discord_intents_required", extra={"context": {
            "reason": "Enable Server Members Intent in the Developer Portal when welcome is configured.",
        }})
        return 1
    except (discord.DiscordException, OSError) as exc:
        logger.error("discord_startup_failed", extra={"context": {"error_type": type(exc).__name__,
                                                                 "status": getattr(exc, "status", None)}})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
