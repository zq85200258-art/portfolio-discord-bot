"""Persist subscriptions and delivered article identities in SQLite."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Subscription:
    id: int
    guild_id: int
    channel_id: int
    url: str


class SubscriptionStore:
    """Keep delivery history independent for every guild, channel, and feed."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    UNIQUE (guild_id, channel_id, url)
                );
                CREATE TABLE IF NOT EXISTS seen (
                    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                    article_key TEXT NOT NULL,
                    PRIMARY KEY (subscription_id, article_key)
                );
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection

    def add(self, guild_id: int, channel_id: int, url: str, initial_keys: Iterable[str]) -> int | None:
        """Insert a new subscription and record its existing feed entries atomically."""
        with self._connect() as connection:
            row = connection.execute(
                """INSERT INTO subscriptions (guild_id, channel_id, url) VALUES (?, ?, ?)
                   ON CONFLICT (guild_id, channel_id, url) DO NOTHING RETURNING id""",
                (guild_id, channel_id, url),
            ).fetchone()
            if row is None:
                return None
            subscription_id = int(row[0])
            self._record(connection, subscription_id, initial_keys)
            return subscription_id

    def remove(self, guild_id: int, subscription_id: int) -> bool:
        """Delete a subscription only if it belongs to the invoking server."""
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM subscriptions WHERE guild_id = ? AND id = ?", (guild_id, subscription_id)
            )
            return cursor.rowcount == 1

    def list(self, guild_id: int | None = None) -> list[Subscription]:
        """Return the server's subscriptions, or the full polling queue."""
        sql = "SELECT id, guild_id, channel_id, url FROM subscriptions"
        parameters = () if guild_id is None else (guild_id,)
        if guild_id is not None:
            sql += " WHERE guild_id = ?"
        with self._connect() as connection:
            return [Subscription(*row) for row in connection.execute(f"{sql} ORDER BY id", parameters)]

    def exists(self, subscription_id: int) -> bool:
        """Check whether a subscription is still active just before delivery."""
        with self._connect() as connection:
            row = connection.execute("SELECT 1 FROM subscriptions WHERE id = ?", (subscription_id,)).fetchone()
            return row is not None

    def seen_keys(self, subscription_id: int) -> set[str]:
        """Read the article keys already recorded for this subscription."""
        with self._connect() as connection:
            return {key for (key,) in connection.execute(
                "SELECT article_key FROM seen WHERE subscription_id = ?", (subscription_id,)
            )}

    def mark_seen(self, subscription_id: int, keys: Iterable[str]) -> None:
        """Record handled article keys, allowing for a concurrent unsubscribe."""
        with self._connect() as connection:
            self._record(connection, subscription_id, keys)

    @staticmethod
    def _record(connection: sqlite3.Connection, subscription_id: int, keys: Iterable[str]) -> None:
        connection.executemany(
            "INSERT OR IGNORE INTO seen SELECT id, ? FROM subscriptions WHERE id = ?",
            ((key, subscription_id) for key in keys),
        )
