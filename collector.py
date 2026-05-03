"""ADB経由でChromeタブを収集してSQLiteに保存する。

仕様は SPEC.md セクション3「Collector仕様」を参照。
collector自体は同期的なADB操作が中心だが、DBアクセス層が aiosqlite なので
最上位で asyncio.run() する形を取る。
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from db import (
    DEFAULT_DB_PATH,
    add_sighting,
    get_db,
    init_db,
    upsert_device,
    upsert_tab,
)
from url_utils import normalize_url, url_hash

log = logging.getLogger(__name__)

ADB_TIMEOUT_SEC = 10.0
HTTP_TIMEOUT_SEC = 5.0
BASE_PORT = 9222
CHROME_SOCKET = "chrome_devtools_remote"

EXCLUDED_URL_SCHEMES: tuple[str, ...] = (
    "chrome:",
    "chrome-search:",
    "chrome-extension:",
    "chrome-untrusted:",
    "about:",
    "devtools:",
    "data:",
    "view-source:",
)


class CollectorError(Exception):
    """Collector起因のエラー（adb/devtoolsどちらも）。"""


class AdbError(CollectorError):
    pass


class DevToolsError(CollectorError):
    pass


@dataclass
class CollectedTab:
    url: str
    title: str | None


@dataclass
class CollectorReport:
    devices_processed: int = 0
    tabs_collected: int = 0
    tabs_new: int = 0
    errors: list[str] = field(default_factory=list)


# ---- subprocess / httpx (同期) ----


def _run_adb(args: list[str], timeout: float = ADB_TIMEOUT_SEC) -> str:
    """adbを実行してstdoutを返す。失敗時は AdbError。"""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as e:
        raise AdbError(f"adb not found: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise AdbError(f"adb timeout: {' '.join(args)}") from e
    if result.returncode != 0:
        raise AdbError(
            f"adb failed ({result.returncode}): {' '.join(args)}: {result.stderr.strip()}"
        )
    return result.stdout


def _fetch_devtools_tabs(port: int, timeout: float = HTTP_TIMEOUT_SEC) -> list[dict[str, Any]]:
    """`http://localhost:{port}/json/list` からタブ一覧を取得。"""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"http://localhost:{port}/json/list")
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        raise DevToolsError(f"DevTools fetch failed: {e}") from e
    if not isinstance(data, list):
        raise DevToolsError(f"unexpected DevTools response: {type(data).__name__}")
    return data


# ---- 純粋な変換関数 ----


def parse_adb_devices(stdout: str) -> list[str]:
    """`adb devices` の出力から 'device' 状態のシリアルだけ抽出。"""
    serials: list[str] = []
    for raw in stdout.splitlines()[1:]:  # "List of devices attached" を飛ばす
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def parse_unix_sockets(stdout: str) -> list[str]:
    """/proc/net/unix の最終列から abstract socket 名（@で始まる）を抽出。"""
    names: list[str] = []
    for line in stdout.splitlines()[1:]:  # ヘッダ行を飛ばす
        parts = line.split()
        if not parts:
            continue
        path = parts[-1]
        if path.startswith("@"):
            names.append(path[1:])
    return names


def has_chrome_socket(sockets: list[str]) -> bool:
    return CHROME_SOCKET in sockets


def filter_collected_tabs(raw: list[dict[str, Any]]) -> list[CollectedTab]:
    """type=='page' かつ 除外スキーム以外のタブだけ残す。"""
    result: list[CollectedTab] = []
    for entry in raw:
        if entry.get("type") != "page":
            continue
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            continue
        if any(url.startswith(s) for s in EXCLUDED_URL_SCHEMES):
            continue
        title = entry.get("title")
        result.append(CollectedTab(url=url, title=title or None))
    return result


# ---- 端末ごとの処理（同期） ----


def _list_devices() -> list[str]:
    return parse_adb_devices(_run_adb(["adb", "devices"]))


def _get_device_model(serial: str) -> str | None:
    try:
        out = _run_adb(["adb", "-s", serial, "shell", "getprop", "ro.product.model"])
    except AdbError as e:
        log.warning("model取得失敗 %s: %s", serial, e)
        return None
    return out.strip() or None


def _list_unix_sockets(serial: str) -> list[str]:
    out = _run_adb(["adb", "-s", serial, "shell", "cat", "/proc/net/unix"])
    return parse_unix_sockets(out)


def _adb_forward(serial: str, port: int) -> None:
    _run_adb(
        ["adb", "-s", serial, "forward", f"tcp:{port}", f"localabstract:{CHROME_SOCKET}"]
    )


def _adb_unforward(port: int) -> None:
    try:
        _run_adb(["adb", "forward", "--remove", f"tcp:{port}"])
    except AdbError as e:
        log.warning("forward解除失敗 port=%d: %s", port, e)


# ---- メインフロー ----


async def _persist_device_tabs(
    db_path: str,
    serial: str,
    model: str | None,
    tabs: list[CollectedTab],
    now: int,
) -> int:
    """このデバイスで検出したタブを保存し、新規追加件数を返す。"""
    new_count = 0
    async with get_db(db_path) as conn:
        device_id = await upsert_device(conn, serial, model, now)

        existing: set[str] = set()
        if tabs:
            hashes = [url_hash(t.url) for t in tabs]
            placeholders = ",".join("?" * len(hashes))
            cur = await conn.execute(
                f"SELECT url_hash FROM tabs WHERE url_hash IN ({placeholders})",
                hashes,
            )
            existing = {row[0] for row in await cur.fetchall()}

        for tab in tabs:
            h = url_hash(tab.url)
            tab_id = await upsert_tab(conn, normalize_url(tab.url), h, tab.title, now)
            await add_sighting(conn, tab_id, device_id, now, tab_active=True)
            if h not in existing:
                new_count += 1

        await conn.commit()
    return new_count


async def collect_async(db_path: str = DEFAULT_DB_PATH) -> CollectorReport:
    """全デバイスを巡回してタブを収集。エラーは個別 errors に記録して継続。"""
    report = CollectorReport()
    await init_db(db_path)

    try:
        serials = _list_devices()
    except AdbError as e:
        report.errors.append(str(e))
        return report

    for index, serial in enumerate(serials):
        port = BASE_PORT + index
        forwarded = False
        try:
            sockets = _list_unix_sockets(serial)
            if not has_chrome_socket(sockets):
                msg = f"{serial}: chrome socket not found (Chromeを起動してください)"
                log.info(msg)
                report.errors.append(msg)
                continue

            model = _get_device_model(serial)
            _adb_forward(serial, port)
            forwarded = True
            raw = _fetch_devtools_tabs(port)

            tabs = filter_collected_tabs(raw)
            now = int(time.time())
            new_count = await _persist_device_tabs(db_path, serial, model, tabs, now)

            report.tabs_collected += len(tabs)
            report.tabs_new += new_count
            report.devices_processed += 1

        except CollectorError as e:
            log.warning("デバイス処理失敗 %s: %s", serial, e)
            report.errors.append(f"{serial}: {e}")
        except Exception as e:  # noqa: BLE001 - 想定外でも落ちずに次のデバイスへ
            log.exception("デバイス処理で予期しないエラー %s", serial)
            report.errors.append(f"{serial}: {e}")
        finally:
            if forwarded:
                _adb_unforward(port)

    return report


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    report = asyncio.run(collect_async())
    print(f"処理デバイス数 : {report.devices_processed}")
    print(f"検出タブ数     : {report.tabs_collected}")
    print(f"新規タブ数     : {report.tabs_new}")
    if report.errors:
        print(f"エラー ({len(report.errors)}件):")
        for e in report.errors:
            print(f"  - {e}")


if __name__ == "__main__":
    main()
