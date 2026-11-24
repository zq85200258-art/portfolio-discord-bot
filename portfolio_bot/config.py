"""Load and validate the bot's non-secret JSON settings."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Any


class ConfigError(ValueError):
    """An invalid or unreadable configuration file."""


@dataclass(frozen=True, slots=True)
class WelcomeConfig:
    channel_id: int | None
    title: str
    message: str


@dataclass(frozen=True, slots=True)
class RSSConfig:
    interval_seconds: int
    database_path: Path


@dataclass(frozen=True, slots=True)
class LogConfig:
    level: str
    file_path: Path
    max_bytes: int
    backup_count: int


@dataclass(frozen=True, slots=True)
class Config:
    sync_guild_id: int | None
    welcome: WelcomeConfig
    self_assignable_role_ids: frozenset[int]
    rss: RSSConfig
    logging: LogConfig


def _section(values: dict[str, Any], key: str) -> dict[str, Any]:
    section = values.get(key)
    if not isinstance(section, dict):
        raise ConfigError(f"{key} must be an object")
    return section


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigError(f"{field} must be an integer from {minimum} to {maximum}")
    return value


def _id(value: Any, field: str) -> int:
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        value = int(value)
    return _integer(value, field, 1, 2**63 - 1)


def _optional_id(value: Any, field: str) -> int | None:
    return None if value is None else _id(value, field)


def _welcome_template(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty string")
    try:
        for _, name, spec, conversion in Formatter().parse(value):
            if name is not None and (name not in {"server", "mention", "member"} or spec or conversion):
                raise ValueError
    except ValueError as exc:
        raise ConfigError(f"{field} supports only {{server}}, {{mention}}, and {{member}}") from exc
    return value


def _path(root: Path, value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a project-relative file path")
    relative = Path(value)
    resolved = (root / relative).resolve()
    if relative.anchor or resolved == root or not resolved.is_relative_to(root):
        raise ConfigError(f"{field} must point to a file inside the project")
    return resolved


def load_config(path: Path) -> Config:
    """Read JSON and resolve storage paths relative to the configuration file."""
    path = path.resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Could not read {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("The configuration root must be an object")

    welcome = _section(data, "welcome")
    rss = _section(data, "rss")
    logs = _section(data, "logging")
    roles = data.get("self_assignable_role_ids")
    if not isinstance(roles, list):
        raise ConfigError("self_assignable_role_ids must be an array of Discord role IDs")
    role_ids = frozenset(_id(role, "role ID") for role in roles)
    if len(role_ids) > 25:
        raise ConfigError("self_assignable_role_ids can contain at most 25 roles for the Discord menu")
    level = logs.get("level")
    if not isinstance(level, str) or level not in logging.getLevelNamesMapping():
        raise ConfigError("logging.level must be a Python logging level such as INFO")

    database_path = _path(path.parent, rss.get("database_path"), "rss.database_path")
    log_path = _path(path.parent, logs.get("file_path"), "logging.file_path")
    if database_path == log_path:
        raise ConfigError("rss.database_path and logging.file_path must be different")
    return Config(
        sync_guild_id=_optional_id(data.get("sync_guild_id"), "sync_guild_id"),
        welcome=WelcomeConfig(
            channel_id=_optional_id(welcome.get("channel_id"), "welcome.channel_id"),
            title=_welcome_template(welcome.get("title"), "welcome.title"),
            message=_welcome_template(welcome.get("message"), "welcome.message"),
        ),
        self_assignable_role_ids=role_ids,
        rss=RSSConfig(
            interval_seconds=_integer(rss.get("interval_seconds"), "rss.interval_seconds", 30, 86400),
            database_path=database_path,
        ),
        logging=LogConfig(
            level=level,
            file_path=log_path,
            max_bytes=_integer(logs.get("max_bytes"), "logging.max_bytes", 1024, 100_000_000),
            backup_count=_integer(logs.get("backup_count"), "logging.backup_count", 1, 30),
        ),
    )
