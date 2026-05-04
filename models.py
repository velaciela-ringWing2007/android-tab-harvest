"""データクラス定義。

DBスキーマは SPEC.md セクション1 を参照。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

TabStatus = Literal["unread", "read", "later", "archived"]


@dataclass
class Device:
    serial: str
    nickname: str | None
    model: str | None
    added_at: int
    id: int | None = None


@dataclass
class Tag:
    name: str
    id: int | None = None


@dataclass
class Tab:
    url: str
    url_hash: str
    title: str | None
    status: TabStatus
    note: str | None
    summary: str | None
    summarized_at: int | None
    created_at: int
    updated_at: int
    id: int | None = None
    # UI表示用 (JOINで取得)
    devices: list[str] = field(default_factory=list)
    sighting_count: int = 0
    first_seen: int | None = None
    still_open: bool = False
    tags: list[Tag] = field(default_factory=list)
    summary_long: str | None = None
    summarized_long_at: int | None = None


@dataclass
class TabSighting:
    tab_id: int
    device_id: int
    seen_at: int
    tab_active: bool
    id: int | None = None


@dataclass
class DeviceWithStats:
    """端末ページ用：基本情報 + sighting 集計値。"""

    id: int
    serial: str
    nickname: str | None
    model: str | None
    added_at: int
    tab_count: int
    sighting_count: int
    last_seen: int | None
    first_seen: int | None
