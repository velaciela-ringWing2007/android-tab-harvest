"""SQLite初期化とコネクション管理。クエリヘルパもここに集約。

スキーマは SPEC.md セクション1、フィルタ仕様は SPEC.md セクション4 を参照。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Literal

import aiosqlite

from models import Device, DeviceWithStats, Tab, TabStatus, Tag

DEFAULT_DB_PATH = "tabs.db"

SortKey = Literal["updated", "created", "sightings"]

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

-- 全文検索: trigram トークナイザで日本語も含む3文字以上をマッチ。
-- standard FTS（external contentではない）。tabs と同期するためにトリガで二重管理する。
-- external content にすると delete/update のたびに 'delete' コマンドが必要で、
-- 中間状態で disk image malformed を起こしやすかったため通常モードに統一。
CREATE VIRTUAL TABLE IF NOT EXISTS tabs_fts USING fts5(
  title, url, note,
  tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS tabs_ai AFTER INSERT ON tabs BEGIN
  INSERT INTO tabs_fts(rowid, title, url, note)
    VALUES (new.id, new.title, new.url, new.note);
END;
CREATE TRIGGER IF NOT EXISTS tabs_ad AFTER DELETE ON tabs BEGIN
  DELETE FROM tabs_fts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS tabs_au AFTER UPDATE ON tabs BEGIN
  DELETE FROM tabs_fts WHERE rowid = old.id;
  INSERT INTO tabs_fts(rowid, title, url, note)
    VALUES (new.id, new.title, new.url, new.note);
END;
"""

FTS_MIN_CHARS = 3


async def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """テーブルとインデックスを作成（既存なら何もしない）。

    既存DBへの後付けでFTSテーブルが空なら、tabs から一括でバックフィルする。
    """
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(_SCHEMA)
        cur = await conn.execute("SELECT COUNT(*) FROM tabs")
        tabs_count = (await cur.fetchone())[0]
        cur = await conn.execute("SELECT COUNT(*) FROM tabs_fts")
        fts_count = (await cur.fetchone())[0]
        if tabs_count > 0 and fts_count == 0:
            await conn.execute(
                "INSERT INTO tabs_fts(rowid, title, url, note) "
                "SELECT id, title, url, note FROM tabs"
            )
        await conn.commit()


def _fts_phrase(q: str) -> str:
    """FTS5 にユーザー入力を安全に渡すため、引用符で囲んだフレーズに変換。"""
    return '"' + q.replace('"', '""') + '"'


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


# ---- Web UI 用クエリヘルパ ----


def _row_to_tab(row: aiosqlite.Row | tuple) -> Tab:
    """list_tabs / get_tab で組み立てた行を Tab dataclass に詰める。"""
    devices_raw: str | None = row[10]
    devices_list = [d for d in (devices_raw.split("\x1f") if devices_raw else []) if d]
    return Tab(
        id=row[0],
        url=row[1],
        url_hash=row[2],
        title=row[3],
        status=row[4],
        note=row[5],
        summary=row[6],
        summarized_at=row[7],
        created_at=row[8],
        updated_at=row[9],
        devices=devices_list,
        sighting_count=row[11] or 0,
        first_seen=row[12],
        still_open=bool(row[13]) if row[13] is not None else False,
    )


_BASE_SELECT = """
SELECT
  t.id, t.url, t.url_hash, t.title, t.status, t.note,
  t.summary, t.summarized_at, t.created_at, t.updated_at,
  (SELECT GROUP_CONCAT(name, CHAR(31)) FROM (
     SELECT DISTINCT COALESCE(d.nickname, d.model, d.serial) AS name
     FROM tab_sightings s JOIN devices d ON d.id = s.device_id
     WHERE s.tab_id = t.id
   )) AS devices,
  (SELECT COUNT(*) FROM tab_sightings s WHERE s.tab_id = t.id) AS sighting_count,
  (SELECT MIN(s.seen_at) FROM tab_sightings s WHERE s.tab_id = t.id) AS first_seen,
  (SELECT s.tab_active FROM tab_sightings s WHERE s.tab_id = t.id
     ORDER BY s.seen_at DESC LIMIT 1) AS still_open
FROM tabs t
"""


def _build_filter(
    status: str | None,
    device_id: int | None,
    tag: str | None,
    q: str | None,
) -> tuple[str, list[object]]:
    """WHERE句と引数リストを組み立てる（先頭に WHERE は付けない）。"""
    clauses: list[str] = []
    params: list[object] = []

    if status:
        clauses.append("t.status = ?")
        params.append(status)
    if device_id is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM tab_sightings s WHERE s.tab_id = t.id AND s.device_id = ?)"
        )
        params.append(device_id)
    if tag:
        clauses.append(
            "EXISTS (SELECT 1 FROM tab_tags tt JOIN tags g ON tt.tag_id = g.id "
            "WHERE tt.tab_id = t.id AND g.name = ?)"
        )
        params.append(tag)
    if q:
        q_stripped = q.strip()
        if len(q_stripped) >= FTS_MIN_CHARS:
            # 3文字以上は FTS5（trigram） で高速マッチ
            clauses.append(
                "t.id IN (SELECT rowid FROM tabs_fts WHERE tabs_fts MATCH ?)"
            )
            params.append(_fts_phrase(q_stripped))
        else:
            # 短いクエリは trigram にならないので LIKE フォールバック
            clauses.append(
                "(COALESCE(t.title,'') LIKE ? OR t.url LIKE ? OR COALESCE(t.note,'') LIKE ?)"
            )
            like = f"%{q_stripped}%"
            params.extend([like, like, like])

    return (" AND ".join(clauses), params)


_SORT_SQL: dict[str, str] = {
    "updated": "t.updated_at DESC, t.id DESC",
    "created": "t.created_at DESC, t.id DESC",
    "sightings": "sighting_count DESC, t.updated_at DESC, t.id DESC",
}


async def list_tabs(
    conn: aiosqlite.Connection,
    *,
    status: str | None = None,
    device_id: int | None = None,
    tag: str | None = None,
    q: str | None = None,
    sort: SortKey = "updated",
    page: int = 1,
    per_page: int = 50,
) -> list[Tab]:
    where_sql, params = _build_filter(status, device_id, tag, q)
    sql = _BASE_SELECT
    if where_sql:
        sql += f" WHERE {where_sql}"
    sql += f" ORDER BY {_SORT_SQL.get(sort, _SORT_SQL['updated'])}"
    sql += " LIMIT ? OFFSET ?"
    params = [*params, per_page, max(0, (page - 1) * per_page)]

    cur = await conn.execute(sql, params)
    rows = await cur.fetchall()
    return [_row_to_tab(r) for r in rows]


async def count_tabs(
    conn: aiosqlite.Connection,
    *,
    status: str | None = None,
    device_id: int | None = None,
    tag: str | None = None,
    q: str | None = None,
) -> int:
    where_sql, params = _build_filter(status, device_id, tag, q)
    sql = "SELECT COUNT(*) FROM tabs t"
    if where_sql:
        sql += f" WHERE {where_sql}"
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def status_counts(conn: aiosqlite.Connection) -> dict[str, int]:
    """ステータスごとの件数。フィルタバーの数字表示用。"""
    cur = await conn.execute("SELECT status, COUNT(*) FROM tabs GROUP BY status")
    rows = await cur.fetchall()
    counts = {status: 0 for status in ("unread", "read", "later", "archived")}
    for status, count in rows:
        counts[status] = count
    return counts


async def get_tab(conn: aiosqlite.Connection, tab_id: int) -> Tab | None:
    cur = await conn.execute(_BASE_SELECT + " WHERE t.id = ?", (tab_id,))
    row = await cur.fetchone()
    return _row_to_tab(row) if row else None


async def update_tab_status(
    conn: aiosqlite.Connection, tab_id: int, status: TabStatus, now: int
) -> None:
    await conn.execute(
        "UPDATE tabs SET status = ?, updated_at = ? WHERE id = ?",
        (status, now, tab_id),
    )


async def update_tab_note(
    conn: aiosqlite.Connection, tab_id: int, note: str | None, now: int
) -> None:
    await conn.execute(
        "UPDATE tabs SET note = ?, updated_at = ? WHERE id = ?",
        (note or None, now, tab_id),
    )


async def delete_tab(conn: aiosqlite.Connection, tab_id: int) -> None:
    await conn.execute("DELETE FROM tabs WHERE id = ?", (tab_id,))


async def bulk_update_status(
    conn: aiosqlite.Connection,
    tab_ids: list[int],
    status: TabStatus,
    now: int,
) -> int:
    """指定IDのタブを一括でステータス変更。更新件数を返す。"""
    if not tab_ids:
        return 0
    placeholders = ",".join("?" * len(tab_ids))
    cur = await conn.execute(
        f"UPDATE tabs SET status = ?, updated_at = ? WHERE id IN ({placeholders})",
        [status, now, *tab_ids],
    )
    return cur.rowcount or 0


async def bulk_delete_tabs(
    conn: aiosqlite.Connection, tab_ids: list[int]
) -> int:
    """指定IDのタブを一括削除。CASCADEで sighting も消える。"""
    if not tab_ids:
        return 0
    placeholders = ",".join("?" * len(tab_ids))
    cur = await conn.execute(
        f"DELETE FROM tabs WHERE id IN ({placeholders})", tab_ids
    )
    return cur.rowcount or 0


async def list_devices(conn: aiosqlite.Connection) -> list[Device]:
    cur = await conn.execute(
        "SELECT id, serial, nickname, model, added_at FROM devices ORDER BY id"
    )
    rows = await cur.fetchall()
    return [
        Device(id=r[0], serial=r[1], nickname=r[2], model=r[3], added_at=r[4])
        for r in rows
    ]


async def update_device_nickname(
    conn: aiosqlite.Connection, device_id: int, nickname: str | None
) -> None:
    await conn.execute(
        "UPDATE devices SET nickname = ? WHERE id = ?", (nickname or None, device_id)
    )


async def list_devices_with_stats(
    conn: aiosqlite.Connection,
) -> list[DeviceWithStats]:
    cur = await conn.execute(
        """
        SELECT
          d.id, d.serial, d.nickname, d.model, d.added_at,
          COUNT(DISTINCT s.tab_id) AS tab_count,
          COUNT(s.id) AS sighting_count,
          MAX(s.seen_at) AS last_seen,
          MIN(s.seen_at) AS first_seen
        FROM devices d
        LEFT JOIN tab_sightings s ON s.device_id = d.id
        GROUP BY d.id
        ORDER BY d.id
        """
    )
    rows = await cur.fetchall()
    return [DeviceWithStats(*r) for r in rows]


async def delete_device(conn: aiosqlite.Connection, device_id: int) -> None:
    """端末と関連する sighting を削除。孤立した tab はそのまま残す。"""
    await conn.execute(
        "DELETE FROM tab_sightings WHERE device_id = ?", (device_id,)
    )
    await conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))


async def list_tags(conn: aiosqlite.Connection) -> list[Tag]:
    cur = await conn.execute("SELECT id, name FROM tags ORDER BY name")
    rows = await cur.fetchall()
    return [Tag(id=r[0], name=r[1]) for r in rows]


async def list_tab_tags(conn: aiosqlite.Connection, tab_id: int) -> list[Tag]:
    cur = await conn.execute(
        "SELECT g.id, g.name FROM tags g JOIN tab_tags tt ON tt.tag_id = g.id "
        "WHERE tt.tab_id = ? ORDER BY g.name",
        (tab_id,),
    )
    rows = await cur.fetchall()
    return [Tag(id=r[0], name=r[1]) for r in rows]


async def add_tab_tag(
    conn: aiosqlite.Connection, tab_id: int, tag_name: str
) -> int:
    """タグが無ければ作成し、tab_tags に紐付け。tag_id を返す。"""
    name = tag_name.strip()
    if not name:
        raise ValueError("tag name is empty")
    await conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
    cur = await conn.execute("SELECT id FROM tags WHERE name = ?", (name,))
    row = await cur.fetchone()
    assert row is not None
    tag_id = int(row[0])
    await conn.execute(
        "INSERT OR IGNORE INTO tab_tags (tab_id, tag_id) VALUES (?, ?)", (tab_id, tag_id)
    )
    return tag_id


async def remove_tab_tag(
    conn: aiosqlite.Connection, tab_id: int, tag_id: int
) -> None:
    await conn.execute(
        "DELETE FROM tab_tags WHERE tab_id = ? AND tag_id = ?", (tab_id, tag_id)
    )
