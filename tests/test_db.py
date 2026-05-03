"""DB初期化とスキーマのユニットテスト。

aiosqlite + pytest-asyncio。各テストは一時DBファイルで隔離。
"""

import time
from pathlib import Path

import aiosqlite
import pytest

from db import (
    add_sighting,
    add_tab_tag,
    count_tabs,
    delete_device,
    delete_tab,
    get_db,
    get_tab,
    init_db,
    list_devices,
    list_devices_with_stats,
    list_tab_tags,
    list_tabs,
    list_tags,
    remove_tab_tag,
    status_counts,
    update_device_nickname,
    update_tab_note,
    update_tab_status,
    upsert_device,
    upsert_tab,
)


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


async def _seed(db_path: str) -> dict[str, int]:
    """テスト用に最小のデバイス2台・タブ3つ・sighting・タグを仕込む。"""
    await init_db(db_path)
    ids: dict[str, int] = {}
    async with get_db(db_path) as conn:
        d1 = await upsert_device(conn, "DEV_A", "Pixel A", 1700000000)
        d2 = await upsert_device(conn, "DEV_B", "Pixel B", 1700000000)
        ids["d1"], ids["d2"] = d1, d2

        t1 = await upsert_tab(conn, "https://a.example/x", "h_a", "Article A", 1700000100)
        t2 = await upsert_tab(conn, "https://b.example/y", "h_b", "Article B", 1700000200)
        t3 = await upsert_tab(conn, "https://c.example/z", "h_c", "Searchable Note", 1700000300)
        ids["t1"], ids["t2"], ids["t3"] = t1, t2, t3

        # sightings: t1=DEV_A, t2=DEV_A+DEV_B, t3=DEV_B
        await add_sighting(conn, t1, d1, 1700000100, tab_active=True)
        await add_sighting(conn, t2, d1, 1700000200, tab_active=True)
        await add_sighting(conn, t2, d2, 1700000201, tab_active=False)  # 最新がfalse=閉じ済
        await add_sighting(conn, t3, d2, 1700000300, tab_active=True)

        # t2 にタグ
        await add_tab_tag(conn, t2, "tech")
        await conn.commit()
    return ids


class TestListTabs:
    async def test_returns_all_when_no_filter(self, db_path: str) -> None:
        await _seed(db_path)
        async with get_db(db_path) as conn:
            tabs = await list_tabs(conn)
        assert len(tabs) == 3

    async def test_filters_by_status(self, db_path: str) -> None:
        ids = await _seed(db_path)
        now = 1700000999
        async with get_db(db_path) as conn:
            await update_tab_status(conn, ids["t1"], "read", now)
            await conn.commit()
            tabs = await list_tabs(conn, status="unread")
        assert {t.id for t in tabs} == {ids["t2"], ids["t3"]}

    async def test_filters_by_device(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            tabs = await list_tabs(conn, device_id=ids["d1"])
        assert {t.id for t in tabs} == {ids["t1"], ids["t2"]}

    async def test_filters_by_tag(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            tabs = await list_tabs(conn, tag="tech")
        assert {t.id for t in tabs} == {ids["t2"]}

    async def test_q_matches_title_or_note(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            tabs = await list_tabs(conn, q="Searchable")
        assert {t.id for t in tabs} == {ids["t3"]}

    async def test_q_matches_url(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            tabs = await list_tabs(conn, q="b.example")
        assert {t.id for t in tabs} == {ids["t2"]}

    async def test_sort_updated_desc(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            tabs = await list_tabs(conn, sort="updated")
        assert [t.id for t in tabs] == [ids["t3"], ids["t2"], ids["t1"]]

    async def test_pagination(self, db_path: str) -> None:
        await _seed(db_path)
        async with get_db(db_path) as conn:
            page1 = await list_tabs(conn, per_page=2, page=1)
            page2 = await list_tabs(conn, per_page=2, page=2)
        assert len(page1) == 2
        assert len(page2) == 1
        assert {t.id for t in page1}.isdisjoint({t.id for t in page2})

    async def test_returns_devices_and_sighting_count(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            tabs = await list_tabs(conn)
            t2 = next(t for t in tabs if t.id == ids["t2"])
        # t2 は DEV_A, DEV_B 両方で検出
        assert sorted(t2.devices) == ["Pixel A", "Pixel B"]
        assert t2.sighting_count == 2

    async def test_still_open_reflects_latest_sighting(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            tabs = await list_tabs(conn)
            t1 = next(t for t in tabs if t.id == ids["t1"])
            t2 = next(t for t in tabs if t.id == ids["t2"])
        assert t1.still_open is True
        assert t2.still_open is False  # 最新が active=False


class TestCountAndStatusCounts:
    async def test_count_with_filter(self, db_path: str) -> None:
        await _seed(db_path)
        async with get_db(db_path) as conn:
            assert await count_tabs(conn) == 3
            assert await count_tabs(conn, tag="tech") == 1

    async def test_status_counts(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            await update_tab_status(conn, ids["t1"], "read", 1700001000)
            await update_tab_status(conn, ids["t3"], "later", 1700001000)
            await conn.commit()
            counts = await status_counts(conn)
        assert counts == {"unread": 1, "read": 1, "later": 1, "archived": 0}


class TestUpdateAndDelete:
    async def test_update_status(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            await update_tab_status(conn, ids["t1"], "archived", 1700001000)
            await conn.commit()
            tab = await get_tab(conn, ids["t1"])
        assert tab is not None
        assert tab.status == "archived"
        assert tab.updated_at == 1700001000

    async def test_update_note(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            await update_tab_note(conn, ids["t1"], "あとで読む", 1700001000)
            await conn.commit()
            tab = await get_tab(conn, ids["t1"])
        assert tab is not None
        assert tab.note == "あとで読む"

    async def test_update_note_empty_becomes_null(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            await update_tab_note(conn, ids["t1"], "memo", 1700001000)
            await update_tab_note(conn, ids["t1"], "", 1700001001)
            await conn.commit()
            tab = await get_tab(conn, ids["t1"])
        assert tab is not None
        assert tab.note is None

    async def test_delete_tab_cascades_sightings(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            await delete_tab(conn, ids["t2"])
            await conn.commit()
            cur = await conn.execute(
                "SELECT COUNT(*) FROM tab_sightings WHERE tab_id = ?", (ids["t2"],)
            )
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 0


class TestTags:
    async def test_add_creates_tag(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            tag_id = await add_tab_tag(conn, ids["t1"], "newtag")
            await conn.commit()
            tags = await list_tab_tags(conn, ids["t1"])
        assert any(t.id == tag_id and t.name == "newtag" for t in tags)

    async def test_add_reuses_existing_tag(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            id1 = await add_tab_tag(conn, ids["t1"], "shared")
            id2 = await add_tab_tag(conn, ids["t2"], "shared")
            await conn.commit()
        assert id1 == id2

    async def test_remove_tag(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            tag_id = await add_tab_tag(conn, ids["t1"], "removeme")
            await conn.commit()
            await remove_tab_tag(conn, ids["t1"], tag_id)
            await conn.commit()
            tags = await list_tab_tags(conn, ids["t1"])
        assert all(t.id != tag_id for t in tags)

    async def test_list_all_tags(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            await add_tab_tag(conn, ids["t1"], "alpha")
            await add_tab_tag(conn, ids["t3"], "beta")
            await conn.commit()
            all_tags = await list_tags(conn)
        names = {t.name for t in all_tags}
        assert {"tech", "alpha", "beta"} <= names

    async def test_empty_tag_name_rejected(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            with pytest.raises(ValueError):
                await add_tab_tag(conn, ids["t1"], "   ")


class TestDevices:
    async def test_list_devices(self, db_path: str) -> None:
        await _seed(db_path)
        async with get_db(db_path) as conn:
            devices = await list_devices(conn)
        assert {d.serial for d in devices} == {"DEV_A", "DEV_B"}

    async def test_update_nickname(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            await update_device_nickname(conn, ids["d1"], "メイン端末")
            await conn.commit()
            devices = await list_devices(conn)
            d1 = next(d for d in devices if d.id == ids["d1"])
        assert d1.nickname == "メイン端末"

    async def test_update_nickname_empty_becomes_null(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            await update_device_nickname(conn, ids["d1"], "")
            await conn.commit()
            devices = await list_devices(conn)
            d1 = next(d for d in devices if d.id == ids["d1"])
        assert d1.nickname is None

    async def test_list_devices_with_stats(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            stats = await list_devices_with_stats(conn)
        d1 = next(s for s in stats if s.id == ids["d1"])
        d2 = next(s for s in stats if s.id == ids["d2"])
        # d1 は t1, t2 の2タブ
        assert d1.tab_count == 2
        assert d1.sighting_count == 2
        assert d1.first_seen == 1700000100
        assert d1.last_seen == 1700000200
        # d2 は t2, t3 の2タブ
        assert d2.tab_count == 2

    async def test_delete_device_removes_sightings_keeps_tabs(self, db_path: str) -> None:
        ids = await _seed(db_path)
        async with get_db(db_path) as conn:
            await delete_device(conn, ids["d1"])
            await conn.commit()
            cur = await conn.execute(
                "SELECT COUNT(*) FROM tab_sightings WHERE device_id = ?", (ids["d1"],)
            )
            assert (await cur.fetchone())[0] == 0
            cur = await conn.execute("SELECT COUNT(*) FROM devices WHERE id = ?", (ids["d1"],))
            assert (await cur.fetchone())[0] == 0
            # タブは残る
            cur = await conn.execute("SELECT COUNT(*) FROM tabs")
            assert (await cur.fetchone())[0] == 3
