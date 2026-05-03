"""SQLite初期化とコネクション管理。

スキーマは SPEC.md セクション1 を参照。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiosqlite

DEFAULT_DB_PATH = "tabs.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  serial   TEXT NOT NULL UNIQUE,
  nickname TEXT,
  model    TEXT,
  added_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tabs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  url           TEXT NOT NULL,
  url_hash      TEXT NOT NULL UNIQUE,
  title         TEXT,
  status        TEXT NOT NULL DEFAULT 'unread',
  note          TEXT,
  summary       TEXT,
  summarized_at INTEGER,
  created_at    INTEGER NOT NULL,
  updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tab_sightings (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  tab_id     INTEGER NOT NULL REFERENCES tabs(id) ON DELETE CASCADE,
  device_id  INTEGER NOT NULL REFERENCES devices(id),
  seen_at    INTEGER NOT NULL,
  tab_active INTEGER NOT NULL DEFAULT 0,
  UNIQUE(tab_id, device_id, seen_at)
);

CREATE TABLE IF NOT EXISTS tags (
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS tab_tags (
  tab_id INTEGER NOT NULL REFERENCES tabs(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (tab_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_tabs_status ON tabs(status);
CREATE INDEX IF NOT EXISTS idx_tabs_updated ON tabs(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sightings_tab ON tab_sightings(tab_id);
CREATE INDEX IF NOT EXISTS idx_sightings_device ON tab_sightings(device_id);
"""


async def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """テーブルとインデックスを作成（既存なら何もしない）。"""
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(_SCHEMA)
        await conn.commit()


@asynccontextmanager
async def get_db(db_path: str = DEFAULT_DB_PATH) -> AsyncIterator[aiosqlite.Connection]:
    """foreign_keys=ON 付きで開いたDBコネクションを yield する。"""
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("PRAGMA foreign_keys = ON")
        yield conn
