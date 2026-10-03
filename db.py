"""SQLite storage for member addresses and meetups."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS addresses (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    address    TEXT    NOT NULL,
    lat        REAL    NOT NULL,
    lon        REAL    NOT NULL,
    updated_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS meetups (
    message_id   INTEGER PRIMARY KEY,
    guild_id     INTEGER NOT NULL,
    organizer_id INTEGER NOT NULL,
    title        TEXT    NOT NULL,
    mid_lat      REAL,
    mid_lon      REAL,
    mid_label    TEXT
);

CREATE TABLE IF NOT EXISTS meetup_participants (
    message_id INTEGER NOT NULL REFERENCES meetups(message_id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL,
    PRIMARY KEY (message_id, user_id)
);
"""


@dataclass
class Meetup:
    message_id: int
    guild_id: int
    organizer_id: int
    title: str
    mid_lat: float | None
    mid_lon: float | None
    mid_label: str | None


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- addresses -------------------------------------------------------

    def set_address(self, guild_id: int, user_id: int, address: str, lat: float, lon: float) -> None:
        self.conn.execute(
            """INSERT INTO addresses (guild_id, user_id, address, lat, lon)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (guild_id, user_id) DO UPDATE SET
                   address = excluded.address, lat = excluded.lat, lon = excluded.lon,
                   updated_at = CURRENT_TIMESTAMP""",
            (guild_id, user_id, address, lat, lon),
        )
        self.conn.commit()

    def get_address(self, guild_id: int, user_id: int) -> tuple[str, float, float] | None:
        return self.conn.execute(
            "SELECT address, lat, lon FROM addresses WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()

    def delete_address(self, guild_id: int, user_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM addresses WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def coords_for(self, guild_id: int, user_ids: list[int]) -> dict[int, tuple[float, float]]:
        if not user_ids:
            return {}
        placeholders = ",".join("?" * len(user_ids))
        rows = self.conn.execute(
            f"SELECT user_id, lat, lon FROM addresses WHERE guild_id = ? AND user_id IN ({placeholders})",
            (guild_id, *user_ids),
        ).fetchall()
        return {uid: (lat, lon) for uid, lat, lon in rows}

    def registered_users(self, guild_id: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT user_id FROM addresses WHERE guild_id = ? ORDER BY updated_at", (guild_id,)
        ).fetchall()
        return [r[0] for r in rows]

    # --- meetups ---------------------------------------------------------

    def create_meetup(self, message_id: int, guild_id: int, organizer_id: int, title: str) -> None:
        self.conn.execute(
            "INSERT INTO meetups (message_id, guild_id, organizer_id, title) VALUES (?, ?, ?, ?)",
            (message_id, guild_id, organizer_id, title),
        )
        self.conn.execute(
            "INSERT INTO meetup_participants (message_id, user_id) VALUES (?, ?)",
            (message_id, organizer_id),
        )
        self.conn.commit()

    def get_meetup(self, message_id: int) -> Meetup | None:
        row = self.conn.execute(
            """SELECT message_id, guild_id, organizer_id, title, mid_lat, mid_lon, mid_label
               FROM meetups WHERE message_id = ?""",
            (message_id,),
        ).fetchone()
        return Meetup(*row) if row else None

    def participants(self, message_id: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT user_id FROM meetup_participants WHERE message_id = ? ORDER BY rowid",
            (message_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def join_meetup(self, message_id: int, user_id: int) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO meetup_participants (message_id, user_id) VALUES (?, ?)",
            (message_id, user_id),
        )
        if cur.rowcount:
            self._clear_midpoint(message_id)
        self.conn.commit()
        return cur.rowcount > 0

    def leave_meetup(self, message_id: int, user_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM meetup_participants WHERE message_id = ? AND user_id = ?",
            (message_id, user_id),
        )
        if cur.rowcount:
            self._clear_midpoint(message_id)
        self.conn.commit()
        return cur.rowcount > 0

    def set_midpoint(self, message_id: int, lat: float, lon: float, label: str) -> None:
        self.conn.execute(
            "UPDATE meetups SET mid_lat = ?, mid_lon = ?, mid_label = ? WHERE message_id = ?",
            (lat, lon, label, message_id),
        )
        self.conn.commit()

    def _clear_midpoint(self, message_id: int) -> None:
        self.conn.execute(
            "UPDATE meetups SET mid_lat = NULL, mid_lon = NULL, mid_label = NULL WHERE message_id = ?",
            (message_id,),
        )
