"""Collector のユニットテスト。

subprocess (adb) と httpx (Chrome DevTools) は monkeypatch でモック。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import collector
from collector import (
    CollectedTab,
    filter_collected_tabs,
    has_chrome_socket,
    parse_adb_devices,
    parse_unix_sockets,
)
from db import get_db, init_db


class TestParseAdbDevices:
    def test_parses_single_device(self) -> None:
        out = "List of devices attached\nFAKE_SERIAL_001\tdevice\n\n"
        assert parse_adb_devices(out) == ["FAKE_SERIAL_001"]

    def test_parses_multiple_devices(self) -> None:
        out = (
            "List of devices attached\n"
            "ABC123\tdevice\n"
            "DEF456\tdevice\n"
        )
        assert parse_adb_devices(out) == ["ABC123", "DEF456"]

    def test_empty_returns_empty_list(self) -> None:
        assert parse_adb_devices("List of devices attached\n\n") == []

    def test_skips_unauthorized_and_offline(self) -> None:
        out = (
            "List of devices attached\n"
            "ABC123\tdevice\n"
            "BAD1\tunauthorized\n"
            "BAD2\toffline\n"
        )
        assert parse_adb_devices(out) == ["ABC123"]

    def test_handles_emulator(self) -> None:
        out = "List of devices attached\nemulator-5554\tdevice\n"
        assert parse_adb_devices(out) == ["emulator-5554"]


class TestParseUnixSockets:
    def test_extracts_devtools_socket_names(self) -> None:
        # /proc/net/unix の出力（一部）。最終列のパスから @で始まるabstract socket名を抽出
        out = (
            "Num       RefCount Protocol Flags    Type St Inode Path\n"
            "0000 00000002 00000000 00010000 0001 01 12345 @chrome_devtools_remote\n"
            "0000 00000002 00000000 00010000 0001 01 12346 @webview_devtools_remote_1234\n"
            "0000 00000002 00000000 00010000 0001 01 12347 /dev/socket/other\n"
        )
        sockets = parse_unix_sockets(out)
        assert "chrome_devtools_remote" in sockets
        assert "webview_devtools_remote_1234" in sockets
        assert "/dev/socket/other" not in sockets


class TestHasChromeSocket:
    def test_detects_chrome_socket(self) -> None:
        assert has_chrome_socket(["chrome_devtools_remote", "other"]) is True

    def test_returns_false_when_only_webview(self) -> None:
        assert has_chrome_socket(["webview_devtools_remote_1234"]) is False

    def test_returns_false_for_empty(self) -> None:
        assert has_chrome_socket([]) is False


class TestFilterCollectedTabs:
    def test_keeps_page_type_only(self) -> None:
        raw = [
            {"type": "page", "url": "https://example.com/", "title": "Ex"},
            {"type": "background_page", "url": "https://x/", "title": "bg"},
            {"type": "service_worker", "url": "https://x/", "title": "sw"},
        ]
        result = filter_collected_tabs(raw)
        assert result == [CollectedTab(url="https://example.com/", title="Ex")]

    def test_excludes_chrome_scheme(self) -> None:
        raw = [
            {"type": "page", "url": "chrome://newtab/", "title": "New Tab"},
            {"type": "page", "url": "chrome-search://local-ntp/", "title": "x"},
            {"type": "page", "url": "https://example.com/", "title": "Ex"},
        ]
        result = filter_collected_tabs(raw)
        assert result == [CollectedTab(url="https://example.com/", title="Ex")]

    def test_excludes_about_scheme(self) -> None:
        raw = [
            {"type": "page", "url": "about:blank", "title": ""},
            {"type": "page", "url": "https://example.com/", "title": "Ex"},
        ]
        result = filter_collected_tabs(raw)
        assert result == [CollectedTab(url="https://example.com/", title="Ex")]

    def test_handles_missing_title(self) -> None:
        raw = [{"type": "page", "url": "https://example.com/"}]
        result = filter_collected_tabs(raw)
        assert result == [CollectedTab(url="https://example.com/", title=None)]

    def test_skips_entries_without_url(self) -> None:
        raw = [{"type": "page", "title": "no url"}, {"type": "page", "url": "https://x/"}]
        result = filter_collected_tabs(raw)
        assert result == [CollectedTab(url="https://x/", title=None)]


# ---- E2Eに近いフローテスト（adb / httpx をmockしてmainを通す） ----


class FakeAdb:
    """`adb` subprocess.run の置き換え。コマンド配列で挙動を分岐。"""

    def __init__(self, devices_out: str, model: str, sockets_out: str) -> None:
        self.devices_out = devices_out
        self.model = model
        self.sockets_out = sockets_out
        self.calls: list[list[str]] = []
        self.forwards: set[int] = set()

    def __call__(self, args: list[str], timeout: float = 10.0) -> str:
        self.calls.append(args)
        # args は ["adb", "devices"] など
        if args[:2] == ["adb", "devices"]:
            return self.devices_out
        if "shell" in args and "getprop" in args:
            return self.model + "\n"
        if "shell" in args and "cat" in args:
            return self.sockets_out
        if "forward" in args:
            # `adb -s SER forward tcp:9222 localabstract:...` または
            # `adb forward --remove tcp:9222`
            if "--remove" in args:
                port = int(args[-1].split(":")[1])
                self.forwards.discard(port)
            else:
                port = int(args[-2].split(":")[1])
                self.forwards.add(port)
            return ""
        return ""


def make_fake_fetch(payload: list[dict[str, Any]]):
    def _fetch(port: int, timeout: float = 5.0) -> list[dict[str, Any]]:
        return payload
    return _fetch


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "tabs.db")


class TestCollectMain:
    async def test_inserts_tabs_and_sightings(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeAdb(
            devices_out="List of devices attached\nSER1\tdevice\n",
            model="Pixel 9",
            sockets_out=(
                "Num       RefCount Protocol Flags    Type St Inode Path\n"
                "0000 00000002 00000000 00010000 0001 01 12345 @chrome_devtools_remote\n"
            ),
        )
        monkeypatch.setattr(collector, "_run_adb", fake)
        monkeypatch.setattr(
            collector,
            "_fetch_devtools_tabs",
            make_fake_fetch(
                [
                    {"type": "page", "url": "https://a.example/x", "title": "A"},
                    {"type": "page", "url": "https://b.example/y?utm_source=tw", "title": "B"},
                    {"type": "page", "url": "chrome://newtab/", "title": "skip"},
                ]
            ),
        )

        report = await collector.collect_async(db_path=db_path)

        assert report.devices_processed == 1
        assert report.tabs_collected == 2  # chrome:// は除外
        assert report.tabs_new == 2
        assert report.errors == []
        assert fake.forwards == set()  # クリーンアップ済み

        async with get_db(db_path) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM tabs")
            assert (await cur.fetchone())[0] == 2
            cur = await conn.execute("SELECT COUNT(*) FROM tab_sightings")
            assert (await cur.fetchone())[0] == 2
            cur = await conn.execute("SELECT COUNT(*) FROM devices")
            assert (await cur.fetchone())[0] == 1

    async def test_second_run_does_not_duplicate_tabs(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeAdb(
            devices_out="List of devices attached\nSER1\tdevice\n",
            model="Pixel 9",
            sockets_out=(
                "Num       RefCount Protocol Flags    Type St Inode Path\n"
                "0000 00000002 00000000 00010000 0001 01 12345 @chrome_devtools_remote\n"
            ),
        )
        monkeypatch.setattr(collector, "_run_adb", fake)
        monkeypatch.setattr(
            collector,
            "_fetch_devtools_tabs",
            make_fake_fetch(
                [{"type": "page", "url": "https://a.example/x", "title": "A"}]
            ),
        )

        # 1回目
        await collector.collect_async(db_path=db_path)
        # 2回目（seen_atがズレるよう少し細工は不要：t は秒単位で同じになる可能性があるので時刻を進める）
        # collector は内部で time.time() を見ているので、monkeypatchで時刻を進める
        monkeypatch.setattr(collector.time, "time", lambda: 1700000999)
        report2 = await collector.collect_async(db_path=db_path)

        assert report2.tabs_new == 0  # 重複は新規ではない

        async with get_db(db_path) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM tabs")
            assert (await cur.fetchone())[0] == 1
            cur = await conn.execute("SELECT COUNT(*) FROM tab_sightings")
            # sightingは2回分（時刻が違うから）
            assert (await cur.fetchone())[0] == 2

    async def test_no_devices_is_graceful(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeAdb(
            devices_out="List of devices attached\n",
            model="",
            sockets_out="",
        )
        monkeypatch.setattr(collector, "_run_adb", fake)
        monkeypatch.setattr(collector, "_fetch_devtools_tabs", make_fake_fetch([]))

        report = await collector.collect_async(db_path=db_path)
        assert report.devices_processed == 0
        assert report.tabs_collected == 0

    async def test_device_without_chrome_socket_is_skipped(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeAdb(
            devices_out="List of devices attached\nSER1\tdevice\n",
            model="Pixel 9",
            sockets_out=(
                "Num       RefCount Protocol Flags    Type St Inode Path\n"
                "0000 00000002 00000000 00010000 0001 01 12345 @webview_devtools_remote_999\n"
            ),
        )
        monkeypatch.setattr(collector, "_run_adb", fake)
        monkeypatch.setattr(collector, "_fetch_devtools_tabs", make_fake_fetch([]))

        report = await collector.collect_async(db_path=db_path)
        assert report.devices_processed == 0
        assert "chrome socket not found" in " ".join(report.errors).lower()

    async def test_fetch_failure_does_not_crash(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeAdb(
            devices_out="List of devices attached\nSER1\tdevice\n",
            model="Pixel 9",
            sockets_out=(
                "Num       RefCount Protocol Flags    Type St Inode Path\n"
                "0000 00000002 00000000 00010000 0001 01 12345 @chrome_devtools_remote\n"
            ),
        )
        monkeypatch.setattr(collector, "_run_adb", fake)

        def boom(port: int, timeout: float = 5.0) -> list[dict[str, Any]]:
            raise ConnectionError("connection refused")

        monkeypatch.setattr(collector, "_fetch_devtools_tabs", boom)

        report = await collector.collect_async(db_path=db_path)
        assert report.devices_processed == 0
        assert any("connection refused" in e.lower() for e in report.errors)
        # forwardもクリーンアップされている
        assert fake.forwards == set()
