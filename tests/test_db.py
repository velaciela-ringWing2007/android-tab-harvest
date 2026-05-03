"""DB初期化とスキーマのユニットテスト。

aiosqlite + pytest-asyncio。各テストは一時DBファイルで隔離。
"""

import time
from pathlib import Path

import aiosqlite
import pytest

from db import get_db, init_db


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "test_tabs.db")


async def _table_names(conn: aiosqlite.Connection) -> set[str]:
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    rows = await cur.fetchall()
    return {row[0] for row in rows}


async def _index_names(conn: aiosqlite.Connection) -> set[str]:
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
    )
    rows = await cur.fetchall()
    return {row[0] for row in rows}


class TestInitDb:
    async def test_creates_all_tables(self, db_path: str) -> None:
        await init_db(db_path)
        async with get_db(db_path) as conn:
            tables = await _table_names(conn)
        assert {"devices", "tabs", "tab_sightings", "tags", "tab_tags"} <= tables

    async def test_creates_indexes(self, db_path: str) -> None:
        await init_db(db_path)
        async with get_db(db_path) as conn:
            indexes = await _index_names(conn)
        assert {
            "idx_tabs_status",
            "idx_tabs_updated",
            "idx_sightings_tab",
            "idx_sightings_device",
        } <= indexes

    async def test_is_idempotent(self, db_path: str) -> None:
        # 2回呼んでもエラーにならない
        await init_db(db_path)
        await init_db(db_path)
        async with get_db(db_path) as conn:
            tables = await _table_names(conn)
        assert "tabs" in tables


class TestGetDb:
    async def test_yields_usable_connection(self, db_path: str) -> None:
        await init_db(db_path)
        now = int(time.time())
        async with get_db(db_path) as conn:
            await conn.execute(
                "INSERT INTO devices (serial, nickname, model, added_at) VALUES (?, ?, ?, ?)",
                ("ABC123", "Pixel 9", "Pixel 9", now),
            )
            await conn.commit()
            cur = await conn.execute("SELECT serial, nickname FROM devices")
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == "ABC123"
        assert row[1] == "Pixel 9"

    async def test_foreign_keys_enabled(self, db_path: str) -> None:
        # ON DELETE CASCADE が機能するためには PRAGMA foreign_keys=ON が必要
        await init_db(db_path)
        async with get_db(db_path) as conn:
            cur = await conn.execute("PRAGMA foreign_keys")
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 1


class TestSchemaConstraints:
    async def test_url_hash_is_unique(self, db_path: str) -> None:
        await init_db(db_path)
        now = int(time.time())
        async with get_db(db_path) as conn:
            await conn.execute(
                "INSERT INTO tabs (url, url_hash, title, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("https://example.com/", "hash1", "T1", "unread", now, now),
            )
            await conn.commit()
            with pytest.raises(aiosqlite.IntegrityError):
                await conn.execute(
                    "INSERT INTO tabs (url, url_hash, title, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("https://example.com/x", "hash1", "T2", "unread", now, now),
                )
                await conn.commit()

    async def test_insert_or_ignore_skips_duplicate_url_hash(self, db_path: str) -> None:
        await init_db(db_path)
        now = int(time.time())
        async with get_db(db_path) as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO tabs (url, url_hash, title, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("https://example.com/", "hash1", "T1", "unread", now, now),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO tabs (url, url_hash, title, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("https://example.com/", "hash1", "T1-dup", "unread", now, now),
            )
            await conn.commit()
            cur = await conn.execute("SELECT COUNT(*) FROM tabs WHERE url_hash = ?", ("hash1",))
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 1

    async def test_device_serial_is_unique(self, db_path: str) -> None:
        await init_db(db_path)
        now = int(time.time())
        async with get_db(db_path) as conn:
            await conn.execute(
                "INSERT INTO devices (serial, added_at) VALUES (?, ?)",
                ("SAME_SERIAL", now),
            )
            await conn.commit()
            with pytest.raises(aiosqlite.IntegrityError):
                await conn.execute(
                    "INSERT INTO devices (serial, added_at) VALUES (?, ?)",
                    ("SAME_SERIAL", now),
                )
                await conn.commit()

    async def test_tab_sightings_unique_constraint(self, db_path: str) -> None:
        await init_db(db_path)
        now = int(time.time())
        async with get_db(db_path) as conn:
            await conn.execute(
                "INSERT INTO devices (serial, added_at) VALUES (?, ?)", ("D1", now)
            )
            await conn.execute(
                "INSERT INTO tabs (url, url_hash, title, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("https://example.com/", "h", "T", "unread", now, now),
            )
            await conn.commit()
            await conn.execute(
                "INSERT INTO tab_sightings (tab_id, device_id, seen_at, tab_active) VALUES (1, 1, ?, 1)",
                (now,),
            )
            await conn.commit()
            with pytest.raises(aiosqlite.IntegrityError):
                await conn.execute(
                    "INSERT INTO tab_sightings (tab_id, device_id, seen_at, tab_active) VALUES (1, 1, ?, 1)",
                    (now,),
                )
                await conn.commit()

    async def test_cascade_delete_tab_removes_sightings(self, db_path: str) -> None:
        await init_db(db_path)
        now = int(time.time())
        async with get_db(db_path) as conn:
            await conn.execute(
                "INSERT INTO devices (serial, added_at) VALUES (?, ?)", ("D1", now)
            )
            await conn.execute(
                "INSERT INTO tabs (url, url_hash, title, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("https://example.com/", "h", "T", "unread", now, now),
            )
            await conn.execute(
                "INSERT INTO tab_sightings (tab_id, device_id, seen_at, tab_active) VALUES (1, 1, ?, 1)",
                (now,),
            )
            await conn.commit()

            await conn.execute("DELETE FROM tabs WHERE id = 1")
            await conn.commit()

            cur = await conn.execute("SELECT COUNT(*) FROM tab_sightings")
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 0
