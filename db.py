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


async def upsert_device(
    conn: aiosqlite.Connection, serial: str, model: str | None, now: int
) -> int:
    """serialが既存ならそのIDを返す。新規ならINSERTしてIDを返す。

    nickname や model はユーザー領域とみなし、既存レコードは更新しない。
    """
    await conn.execute(
        "INSERT OR IGNORE INTO devices (serial, model, added_at) VALUES (?, ?, ?)",
        (serial, model, now),
    )
    cur = await conn.execute("SELECT id FROM devices WHERE serial = ?", (serial,))
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def upsert_tab(
    conn: aiosqlite.Connection,
    url: str,
    url_hash: str,
    title: str | None,
    now: int,
) -> int:
    """url_hash が既存なら title/updated_at を更新してIDを返す。

    新規なら status='unread', created_at=updated_at=now で INSERT。
    """
    await conn.execute(
        "INSERT OR IGNORE INTO tabs (url, url_hash, title, status, created_at, updated_at) "
        "VALUES (?, ?, ?, 'unread', ?, ?)",
        (url, url_hash, title, now, now),
    )
    # 既存レコードのタイトルは最新のものに追従させる（リネームされる記事もあるので）
    await conn.execute(
        "UPDATE tabs SET title = ?, updated_at = ? WHERE url_hash = ?",
        (title, now, url_hash),
    )
    cur = await conn.execute("SELECT id FROM tabs WHERE url_hash = ?", (url_hash,))
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def add_sighting(
    conn: aiosqlite.Connection,
    tab_id: int,
    device_id: int,
    seen_at: int,
    tab_active: bool,
) -> None:
    """検出履歴を追加。同一(tab_id, device_id, seen_at)が既にあれば無視。"""
    await conn.execute(
        "INSERT OR IGNORE INTO tab_sightings (tab_id, device_id, seen_at, tab_active) "
        "VALUES (?, ?, ?, ?)",
        (tab_id, device_id, seen_at, 1 if tab_active else 0),
    )
