"""FastAPI Web UI for android-tab-harvest.

エンドポイント仕様は SPEC.md セクション4 を参照。
HTMXからの呼び出しは部分HTML（tab_row）を返し、行単位で差し替える。
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
from collector import collect_async
from db import DEFAULT_DB_PATH, get_db, init_db

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
DB_PATH = DEFAULT_DB_PATH
PER_PAGE_DEFAULT = 50
ALLOWED_STATUS = {"unread", "read", "later", "archived"}
ALLOWED_SORT = {"updated", "created", "sightings"}
ALLOWED_ORDER = {"asc", "desc"}


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await init_db(DB_PATH)
    yield


app = FastAPI(lifespan=lifespan, title="android-tab-harvest")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ---- Jinjaフィルタ ----

def _format_date(epoch: int | None) -> str:
    if epoch is None:
        return "-"
    return time.strftime("%m/%d %H:%M", time.localtime(epoch))


def _querystring(params: dict[str, Any]) -> str:
    cleaned = {k: v for k, v in params.items() if v not in (None, "")}
    return ("?" + urlencode(cleaned)) if cleaned else ""


templates.env.filters["fmtdate"] = _format_date
templates.env.filters["querystring"] = _querystring


# ---- 共通ユーティリティ ----


def _now() -> int:
    return int(time.time())


def _validated_filters(
    status: str | None, device: int | None, tag: str | None, q: str | None,
    domain: str | None, sort: str, order: str, page: int, per_page: int,
) -> dict[str, object]:
    if status is not None and status not in ALLOWED_STATUS:
        status = None
    if sort not in ALLOWED_SORT:
        sort = "updated"
    if order not in ALLOWED_ORDER:
        order = "desc"
    page = max(1, page)
    per_page = max(1, min(200, per_page))
    return {
        "status": status, "device": device, "tag": tag, "q": q, "domain": domain,
        "sort": sort, "order": order, "page": page, "per_page": per_page,
    }


async def _load_listing(filters: dict[str, object]) -> dict[str, object]:
    """一覧画面・パーシャルで共通の context をまとめて返す。"""
    async with get_db(DB_PATH) as conn:
        tabs = await db.list_tabs(
            conn,
            status=filters["status"],  # type: ignore[arg-type]
            device_id=filters["device"],  # type: ignore[arg-type]
            tag=filters["tag"],  # type: ignore[arg-type]
            q=filters["q"],  # type: ignore[arg-type]
            domain=filters["domain"],  # type: ignore[arg-type]
            sort=filters["sort"],  # type: ignore[arg-type]
            order=filters["order"],  # type: ignore[arg-type]
            page=filters["page"],  # type: ignore[arg-type]
            per_page=filters["per_page"],  # type: ignore[arg-type]
        )
        total = await db.count_tabs(
            conn,
            status=filters["status"],  # type: ignore[arg-type]
            device_id=filters["device"],  # type: ignore[arg-type]
            tag=filters["tag"],  # type: ignore[arg-type]
            q=filters["q"],  # type: ignore[arg-type]
            domain=filters["domain"],  # type: ignore[arg-type]
        )
    return {"tabs": tabs, "total": total, "filters": filters}


# ---- 画面 ----


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    status: str | None = None,
    device: int | None = None,
    tag: str | None = None,
    q: str | None = None,
    domain: str | None = None,
    sort: str = "updated",
    order: str = "desc",
    page: int = 1,
    per_page: int = PER_PAGE_DEFAULT,
    msg: str | None = None,
):
    filters = _validated_filters(
        status, device, tag, q, domain, sort, order, page, per_page
    )
    listing = await _load_listing(filters)
    async with get_db(DB_PATH) as conn:
        counts = await db.status_counts(conn)
        devices = await db.list_devices(conn)
        all_tags = await db.list_tags(conn)
        all_domains = await db.list_domains(conn)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            **listing,
            "counts": counts,
            "devices": devices,
            "all_tags": all_tags,
            "all_domains": all_domains,
            "msg": msg,
        },
    )


@app.get("/tabs", response_class=HTMLResponse)
async def tabs_partial(
    request: Request,
    status: str | None = None,
    device: int | None = None,
    tag: str | None = None,
    q: str | None = None,
    domain: str | None = None,
    sort: str = "updated",
    order: str = "desc",
    page: int = 1,
    per_page: int = PER_PAGE_DEFAULT,
):
    filters = _validated_filters(
        status, device, tag, q, domain, sort, order, page, per_page
    )
    listing = await _load_listing(filters)
    return templates.TemplateResponse(request, "partials/tab_list.html", listing)


# ---- 個別タブ操作 ----


async def _render_tab_row(request: Request, tab_id: int) -> HTMLResponse:
    async with get_db(DB_PATH) as conn:
        tab = await db.get_tab(conn, tab_id)
    if tab is None:
        raise HTTPException(status_code=404, detail="tab not found")
    return templates.TemplateResponse(request, "partials/tab_row.html", {"tab": tab})


@app.post("/tabs/{tab_id}/status", response_class=HTMLResponse)
async def update_status(request: Request, tab_id: int, status: str = Form(...)):
    if status not in ALLOWED_STATUS:
        raise HTTPException(status_code=400, detail="invalid status")
    async with get_db(DB_PATH) as conn:
        await db.update_tab_status(conn, tab_id, status, _now())  # type: ignore[arg-type]
        await conn.commit()
    return await _render_tab_row(request, tab_id)


@app.post("/tabs/{tab_id}/note", response_class=HTMLResponse)
async def update_note(request: Request, tab_id: int, note: str = Form("")):
    async with get_db(DB_PATH) as conn:
        await db.update_tab_note(conn, tab_id, note, _now())
        await conn.commit()
    return await _render_tab_row(request, tab_id)


@app.delete("/tabs/{tab_id}")
async def delete_tab(tab_id: int):
    async with get_db(DB_PATH) as conn:
        await db.delete_tab(conn, tab_id)
        await conn.commit()
    # HTMX hx-swap="outerHTML" + 空レスポンスで行を消す
    return Response(status_code=200, content="")


@app.post("/tabs/bulk", response_class=HTMLResponse)
async def bulk_action(
    request: Request,
    action: str = Form(...),
    tab_ids: list[int] = Form(default=[]),
    tag_name: str = Form(""),
    tag_id: int | None = Form(None),
    # フィルタ保持用（hidden）
    status: str | None = Form(None),
    device: int | None = Form(None),
    tag: str | None = Form(None),
    q: str | None = Form(None),
    domain: str | None = Form(None),
    sort: str = Form("updated"),
    order: str = Form("desc"),
    page: int = Form(1),
    per_page: int = Form(PER_PAGE_DEFAULT),
):
    if tab_ids:
        if action == "delete":
            async with get_db(DB_PATH) as conn:
                await db.bulk_delete_tabs(conn, tab_ids)
                await conn.commit()
        elif action in ALLOWED_STATUS:
            async with get_db(DB_PATH) as conn:
                await db.bulk_update_status(conn, tab_ids, action, _now())  # type: ignore[arg-type]
                await conn.commit()
        elif action == "tag_add":
            if not tag_name.strip():
                raise HTTPException(status_code=400, detail="tag_name required")
            async with get_db(DB_PATH) as conn:
                try:
                    await db.bulk_add_tag(conn, tab_ids, tag_name)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e)) from e
                await conn.commit()
        elif action == "tag_remove":
            if tag_id is None:
                raise HTTPException(status_code=400, detail="tag_id required")
            async with get_db(DB_PATH) as conn:
                await db.bulk_remove_tag(conn, tab_ids, tag_id)
                await conn.commit()
        else:
            raise HTTPException(status_code=400, detail="invalid action")
    filters = _validated_filters(
        status, device, tag, q, domain, sort, order, page, per_page
    )
    listing = await _load_listing(filters)
    return templates.TemplateResponse(request, "partials/tab_list.html", listing)


@app.post("/tabs/{tab_id}/tags", response_class=HTMLResponse)
async def add_tag(request: Request, tab_id: int, name: str = Form(...)):
    async with get_db(DB_PATH) as conn:
        try:
            await db.add_tab_tag(conn, tab_id, name)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        await conn.commit()
    return await _render_tab_row(request, tab_id)


@app.delete("/tabs/{tab_id}/tags/{tag_id}", response_class=HTMLResponse)
async def remove_tag(request: Request, tab_id: int, tag_id: int):
    async with get_db(DB_PATH) as conn:
        await db.remove_tab_tag(conn, tab_id, tag_id)
        await conn.commit()
    return await _render_tab_row(request, tab_id)


# ---- 端末 ----


@app.get("/devices", response_class=HTMLResponse)
async def devices_view(request: Request):
    async with get_db(DB_PATH) as conn:
        devices = await db.list_devices_with_stats(conn)
    return templates.TemplateResponse(request, "devices.html", {"devices": devices})


@app.post("/devices/{device_id}/nickname")
async def update_nickname(device_id: int, nickname: str = Form("")):
    async with get_db(DB_PATH) as conn:
        await db.update_device_nickname(conn, device_id, nickname)
        await conn.commit()
    return RedirectResponse("/devices", status_code=303)


@app.delete("/devices/{device_id}")
async def delete_device(device_id: int):
    async with get_db(DB_PATH) as conn:
        await db.delete_device(conn, device_id)
        await conn.commit()
    # HTMX hx-swap=outerHTML + 空レスポンスで該当行を消す
    return Response(status_code=200, content="")


# ---- 収集 ----


@app.post("/collect")
async def trigger_collect():
    report = await collect_async(DB_PATH)
    msg = (
        f"処理 {report.devices_processed}台 / 検出 {report.tabs_collected}件 "
        f"(新規 {report.tabs_new}件)"
    )
    if report.errors:
        msg += f" / エラー {len(report.errors)}件"
    return RedirectResponse(f"/?msg={msg}", status_code=303)


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    uvicorn.run("server:app", host="127.0.0.1", port=8765, reload=False)


if __name__ == "__main__":
    main()
