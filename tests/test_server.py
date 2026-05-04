"""FastAPI Web UI のエンドポイントテスト。

httpx.AsyncClient + ASGITransport で server.app を直接叩く。
DB_PATH は tmp_path に差し替えて隔離。
"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

import server
from db import (
    add_sighting,
    add_tab_tag,
    get_db,
    get_tab,
    init_db,
    list_devices,
    list_tab_tags,
    upsert_device,
    upsert_tab,
)


@pytest.fixture
async def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    db_path = str(tmp_path / "tabs.db")
    monkeypatch.setattr(server, "DB_PATH", db_path)
    await init_db(db_path)
    # シード: デバイス1台、タブ3つ、sighting、タグ1
    async with get_db(db_path) as conn:
        d1 = await upsert_device(conn, "DEV_A", "Pixel A", 1700000000)
        t1 = await upsert_tab(conn, "https://a.example/x", "h_a", "Article A", 1700000100)
        t2 = await upsert_tab(conn, "https://b.example/y", "h_b", "Article B", 1700000200)
        t3 = await upsert_tab(conn, "https://c.example/z", "h_c", "別ジャンル記事", 1700000300)
        await add_sighting(conn, t1, d1, 1700000100, tab_active=True)
        await add_sighting(conn, t2, d1, 1700000200, tab_active=True)
        await add_sighting(conn, t3, d1, 1700000300, tab_active=True)
        await add_tab_tag(conn, t2, "tech")
        await conn.commit()

    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestIndex:
    async def test_returns_html_with_all_tabs(self, client: AsyncClient) -> None:
        r = await client.get("/")
        assert r.status_code == 200
        assert "Article A" in r.text
        assert "Article B" in r.text
        assert "別ジャンル記事" in r.text

    async def test_status_filter_changes_listing(self, client: AsyncClient) -> None:
        r = await client.get("/", params={"status": "later"})
        assert r.status_code == 200
        # 全タブは unread なので later は0件
        assert "該当するタブはありません" in r.text

    async def test_search_q_japanese(self, client: AsyncClient) -> None:
        r = await client.get("/", params={"q": "別ジャンル"})
        assert r.status_code == 200
        assert "別ジャンル記事" in r.text
        assert "Article A" not in r.text

    async def test_tag_filter(self, client: AsyncClient) -> None:
        r = await client.get("/", params={"tag": "tech"})
        assert r.status_code == 200
        assert "Article B" in r.text
        assert "Article A" not in r.text


class TestTabsPartial:
    async def test_returns_partial_without_layout(self, client: AsyncClient) -> None:
        r = await client.get(
            "/tabs", headers={"HX-Request": "true"}, params={"status": "unread"}
        )
        assert r.status_code == 200
        # パーシャルなので <html> や topbar は含まない
        assert "<html" not in r.text
        assert "Article A" in r.text


class TestStatusUpdate:
    async def test_marks_read_and_returns_row(self, client: AsyncClient) -> None:
        r = await client.post("/tabs/1/status", data={"status": "read"})
        assert r.status_code == 200
        assert 'id="tab-1"' in r.text
        assert "status-read" in r.text
        async with get_db(server.DB_PATH) as conn:
            tab = await get_tab(conn, 1)
        assert tab is not None
        assert tab.status == "read"

    async def test_invalid_status_400(self, client: AsyncClient) -> None:
        r = await client.post("/tabs/1/status", data={"status": "bogus"})
        assert r.status_code == 400


class TestNote:
    async def test_update_note(self, client: AsyncClient) -> None:
        r = await client.post("/tabs/1/note", data={"note": "あとで読む"})
        assert r.status_code == 200
        async with get_db(server.DB_PATH) as conn:
            tab = await get_tab(conn, 1)
        assert tab is not None
        assert tab.note == "あとで読む"

    async def test_empty_note_clears(self, client: AsyncClient) -> None:
        await client.post("/tabs/1/note", data={"note": "x"})
        r = await client.post("/tabs/1/note", data={"note": ""})
        assert r.status_code == 200
        async with get_db(server.DB_PATH) as conn:
            tab = await get_tab(conn, 1)
        assert tab is not None
        assert tab.note is None


class TestDelete:
    async def test_delete_removes_row(self, client: AsyncClient) -> None:
        r = await client.delete("/tabs/2")
        assert r.status_code == 200
        async with get_db(server.DB_PATH) as conn:
            tab = await get_tab(conn, 2)
        assert tab is None


class TestTags:
    async def test_add_tag_to_tab(self, client: AsyncClient) -> None:
        r = await client.post("/tabs/1/tags", data={"name": "newtag"})
        assert r.status_code == 200
        async with get_db(server.DB_PATH) as conn:
            tags = await list_tab_tags(conn, 1)
        assert any(t.name == "newtag" for t in tags)

    async def test_remove_tag_from_tab(self, client: AsyncClient) -> None:
        await client.post("/tabs/1/tags", data={"name": "removeme"})
        async with get_db(server.DB_PATH) as conn:
            tags = await list_tab_tags(conn, 1)
            tag_id = next(t.id for t in tags if t.name == "removeme")
        r = await client.delete(f"/tabs/1/tags/{tag_id}")
        assert r.status_code == 200
        async with get_db(server.DB_PATH) as conn:
            tags = await list_tab_tags(conn, 1)
        assert all(t.name != "removeme" for t in tags)

    async def test_empty_tag_400(self, client: AsyncClient) -> None:
        r = await client.post("/tabs/1/tags", data={"name": "   "})
        assert r.status_code == 400


class TestDevices:
    async def test_devices_page(self, client: AsyncClient) -> None:
        r = await client.get("/devices")
        assert r.status_code == 200
        assert "Pixel A" in r.text or "DEV_A" in r.text

    async def test_set_nickname_redirects(self, client: AsyncClient) -> None:
        r = await client.post(
            "/devices/1/nickname", data={"nickname": "メイン"}, follow_redirects=False
        )
        assert r.status_code == 303
        async with get_db(server.DB_PATH) as conn:
            devices = await list_devices(conn)
        assert any(d.id == 1 and d.nickname == "メイン" for d in devices)

    async def test_devices_page_shows_stats(self, client: AsyncClient) -> None:
        r = await client.get("/devices")
        assert r.status_code == 200
        assert "タブ" in r.text
        assert "検出回数" in r.text

    async def test_delete_device(self, client: AsyncClient) -> None:
        r = await client.delete("/devices/1")
        assert r.status_code == 200
        async with get_db(server.DB_PATH) as conn:
            devices = await list_devices(conn)
        assert all(d.id != 1 for d in devices)
        async with get_db(server.DB_PATH) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM tabs")
            count = (await cur.fetchone())[0]
        assert count == 3


class TestBulkAction:
    async def test_bulk_mark_read(self, client: AsyncClient) -> None:
        r = await client.post(
            "/tabs/bulk",
            data={
                "action": "read", "tab_ids": ["1", "3"],
                "sort": "updated", "page": "1", "per_page": "50",
            },
        )
        assert r.status_code == 200
        assert "bulk-form" in r.text
        async with get_db(server.DB_PATH) as conn:
            t1 = await get_tab(conn, 1)
            t2 = await get_tab(conn, 2)
            t3 = await get_tab(conn, 3)
        assert t1.status == "read"
        assert t2.status == "unread"  # 対象外
        assert t3.status == "read"

    async def test_bulk_delete(self, client: AsyncClient) -> None:
        r = await client.post(
            "/tabs/bulk",
            data={
                "action": "delete", "tab_ids": ["1", "2"],
                "sort": "updated", "page": "1", "per_page": "50",
            },
        )
        assert r.status_code == 200
        async with get_db(server.DB_PATH) as conn:
            assert await get_tab(conn, 1) is None
            assert await get_tab(conn, 2) is None
            assert await get_tab(conn, 3) is not None

    async def test_bulk_no_selection_returns_listing(self, client: AsyncClient) -> None:
        r = await client.post(
            "/tabs/bulk",
            data={"action": "read", "sort": "updated", "page": "1", "per_page": "50"},
        )
        assert r.status_code == 200
        assert "bulk-form" in r.text

    async def test_bulk_invalid_action_400(self, client: AsyncClient) -> None:
        r = await client.post(
            "/tabs/bulk",
            data={
                "action": "bogus", "tab_ids": ["1"],
                "sort": "updated", "page": "1", "per_page": "50",
            },
        )
        assert r.status_code == 400

    async def test_bulk_preserves_filter_in_response(self, client: AsyncClient) -> None:
        r = await client.post(
            "/tabs/bulk",
            data={
                "action": "read", "tab_ids": ["2"],
                "tag": "tech", "sort": "updated", "page": "1", "per_page": "50",
            },
        )
        assert r.status_code == 200
        assert 'name="tag" value="tech"' in r.text


class TestSortOrder:
    async def test_default_is_desc(self, client: AsyncClient) -> None:
        r = await client.get("/", params={"sort": "updated"})
        assert r.status_code == 200
        # 矢印 ↓ が active チップに含まれる
        assert "↓" in r.text

    async def test_asc_order_renders_arrow_up(self, client: AsyncClient) -> None:
        r = await client.get("/", params={"sort": "updated", "order": "asc"})
        assert r.status_code == 200
        assert "↑" in r.text

    async def test_invalid_order_falls_back_silently(self, client: AsyncClient) -> None:
        r = await client.get("/", params={"sort": "updated", "order": "bogus"})
        assert r.status_code == 200


class TestNotFound:
    async def test_status_on_missing_tab(self, client: AsyncClient) -> None:
        r = await client.post("/tabs/9999/status", data={"status": "read"})
        assert r.status_code == 404
