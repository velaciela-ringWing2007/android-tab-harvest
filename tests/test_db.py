"""DB初期化とスキーマのユニットテスト。

aiosqlite + pytest-asyncio。各テストは一時DBファイルで隔離。
"""

import time
from pathlib import Path

import aiosqlite
import pytest

from db import add_sighting, get_db, init_db, upsert_device, upsert_tab


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


class TestUpsertDevice:
    async def test_inserts_new_device(self, db_path: str) -> None:
        await init_db(db_path)
        async with get_db(db_path) as conn:
            device_id = await upsert_device(conn, "SERIAL_A", "Pixel 9", 1700000000)
            await conn.commit()
        assert device_id == 1

    async def test_returns_existing_id_for_same_serial(self, db_path: str) -> None:
        await init_db(db_path)
        async with get_db(db_path) as conn:
            id1 = await upsert_device(conn, "SERIAL_A", "Pixel 9", 1700000000)
            await conn.commit()
            id2 = await upsert_device(conn, "SERIAL_A", "Pixel 9", 1700000999)
            await conn.commit()
        assert id1 == id2

    async def test_does_not_overwrite_existing_model(self, db_path: str) -> None:
        # 既存デバイスのmodelは更新しない（ユーザー領域）
        await init_db(db_path)
        async with get_db(db_path) as conn:
            await upsert_device(conn, "SERIAL_A", "OriginalModel", 1700000000)
            await conn.commit()
            await upsert_device(conn, "SERIAL_A", "ChangedModel", 1700000999)
            await conn.commit()
            cur = await conn.execute("SELECT model FROM devices WHERE serial = ?", ("SERIAL_A",))
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == "OriginalModel"


class TestUpsertTab:
    async def test_inserts_new_tab_with_unread_status(self, db_path: str) -> None:
        await init_db(db_path)
        async with get_db(db_path) as conn:
            tab_id = await upsert_tab(
                conn, "https://example.com/", "h1", "Title", 1700000000
            )
            await conn.commit()
            cur = await conn.execute(
                "SELECT status, created_at, updated_at FROM tabs WHERE id = ?", (tab_id,)
            )
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == "unread"
        assert row[1] == 1700000000
        assert row[2] == 1700000000

    async def test_returns_same_id_for_same_hash(self, db_path: str) -> None:
        await init_db(db_path)
        async with get_db(db_path) as conn:
            id1 = await upsert_tab(conn, "https://example.com/", "h1", "T1", 1700000000)
            await conn.commit()
            id2 = await upsert_tab(conn, "https://example.com/", "h1", "T2", 1700000999)
            await conn.commit()
        assert id1 == id2

    async def test_updates_title_and_updated_at(self, db_path: str) -> None:
        await init_db(db_path)
        async with get_db(db_path) as conn:
            await upsert_tab(conn, "https://example.com/", "h1", "Old Title", 1700000000)
            await conn.commit()
            await upsert_tab(conn, "https://example.com/", "h1", "New Title", 1700000999)
            await conn.commit()
            cur = await conn.execute(
                "SELECT title, created_at, updated_at FROM tabs WHERE url_hash = ?", ("h1",)
            )
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == "New Title"
        assert row[1] == 1700000000  # created_at は不変
        assert row[2] == 1700000999  # updated_at は更新


class TestAddSighting:
    async def test_records_sighting(self, db_path: str) -> None:
        await init_db(db_path)
        async with get_db(db_path) as conn:
            device_id = await upsert_device(conn, "S1", "M1", 1700000000)
            tab_id = await upsert_tab(conn, "https://example.com/", "h1", "T", 1700000000)
            await add_sighting(conn, tab_id, device_id, 1700000000, tab_active=True)
            await conn.commit()
            cur = await conn.execute(
                "SELECT tab_active FROM tab_sightings WHERE tab_id = ? AND device_id = ?",
                (tab_id, device_id),
            )
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 1

    async def test_duplicate_sighting_is_ignored(self, db_path: str) -> None:
        # 同一(tab_id, device_id, seen_at)はUNIQUEなので二重記録されない
        await init_db(db_path)
        async with get_db(db_path) as conn:
            device_id = await upsert_device(conn, "S1", "M1", 1700000000)
            tab_id = await upsert_tab(conn, "https://example.com/", "h1", "T", 1700000000)
            await add_sighting(conn, tab_id, device_id, 1700000000, tab_active=True)
            await add_sighting(conn, tab_id, device_id, 1700000000, tab_active=False)
            await conn.commit()
            cur = await conn.execute("SELECT COUNT(*) FROM tab_sightings")
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 1
