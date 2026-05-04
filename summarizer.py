"""LM Studio による記事要約 + タグ自動提案。

フロー:
1. URL を httpx で取得（404/dead-link は専用結果）
2. trafilatura で本文抽出
3. LM Studio OpenAI互換API に「3行要約 + タグ最大3」を JSON で依頼
4. tabs.summary / tabs.summarized_at を更新、提案タグを付与

LM Studio 側は "Local Server" を起動しておくこと（デフォルト
http://localhost:1234/v1/chat/completions）。URL は環境変数
LM_STUDIO_URL で上書き可能。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import trafilatura

from db import (
    DEFAULT_DB_PATH,
    add_tab_tag,
    get_db,
    get_tab,
    init_db,
)

log = logging.getLogger(__name__)

LM_STUDIO_URL_DEFAULT = "http://localhost:1234/v1/chat/completions"
HTTP_TIMEOUT = 15.0
LM_TIMEOUT = 180.0
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) android-tab-harvest/1.0 "
    "(+local-only summarizer)"
)
MAX_BODY_CHARS = 6000  # LLMに渡す本文の最大文字数


PROMPT_TEMPLATE = """以下のWebページの本文を読んで日本語で**簡潔に3行**要約し、
記事内容を表すタグを最大3個提案してください。

出力は次の JSON 形式の **JSON のみ**（前置きの説明やコードブロック禁止）:
{{"summary": "3行の要約（改行で区切る）", "tags": ["タグ1", "タグ2"]}}

タイトル: {title}
URL: {url}
本文:
{body}
"""

DETAIL_PROMPT_TEMPLATE = """以下のWebページの本文を**詳しく日本語で要約**してください。

要件:
- **Markdown形式** で出力（見出し `##`、箇条書き `-`、強調 `**…**` などを活用）
- 全体で 5〜10 段落・行程度に分け、長すぎる一文は避ける
- 各セクションの主張・根拠・結論を含める
- 専門用語は適度に補足
- 出力は**Markdownテキストのみ**（コードブロック ``` で全体を囲まない、前置きの説明禁止）

タイトル: {title}
URL: {url}
本文:
{body}
"""


@dataclass
class SummaryResult:
    summary: str
    tags: list[str] = field(default_factory=list)
    error: str | None = None
    is_dead: bool = False


@dataclass
class DetailResult:
    text: str
    error: str | None = None
    is_dead: bool = False


def _lm_url() -> str:
    return os.environ.get("LM_STUDIO_URL", LM_STUDIO_URL_DEFAULT)


# ---- 本文取得 ----


def fetch_body(url: str, timeout: float = HTTP_TIMEOUT) -> tuple[int, str]:
    """URL を取得し (status_code, html_text) を返す。

    例外を投げず、DNS失敗・タイムアウト等は status=-1 で返す。
    """
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = client.get(url)
        return resp.status_code, resp.text
    except httpx.HTTPError as e:
        log.warning("fetch failed %s: %s", url, e)
        return -1, ""


def extract_text(html: str) -> str:
    """HTMLから本文テキストを抽出（trafilatura）、長すぎれば切り詰め。"""
    if not html:
        return ""
    text = (
        trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    )
    return text[:MAX_BODY_CHARS]


# ---- LLM ----


def _normalize_summary(text: str) -> str:
    """LLM出力の各行を strip し、空行を削除した文字列に整える。

    モデルによっては行頭にスペースや箇条書き記号の前置きが入って表示が
    ガタつくことがあるので保存前に正規化する。
    """
    lines = [line.strip() for line in (text or "").splitlines()]
    return "\n".join(line for line in lines if line)


def parse_llm_response(content: str) -> dict[str, Any]:
    """LLMが返した文字列から JSON を取り出す。

    モデルがコードブロック ``` で囲んだり、前後に余計な文章を付けたりするので
    緩めに parse する。
    """
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        i = text.find("{")
        j = text.rfind("}")
        if i >= 0 and j > i:
            return json.loads(text[i : j + 1])
        raise


def _post_chat(prompt: str, lm_url: str | None, timeout: float) -> str:
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "stream": False,
    }
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(lm_url or _lm_url(), json=payload)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def call_llm(
    title: str, url: str, body: str,
    lm_url: str | None = None, timeout: float = LM_TIMEOUT,
) -> dict[str, Any]:
    """LM Studio OpenAI互換APIを呼んでJSONをparseして返す（短い要約用）。"""
    prompt = PROMPT_TEMPLATE.format(title=title or "", url=url, body=body)
    return parse_llm_response(_post_chat(prompt, lm_url, timeout))


def call_llm_text(
    title: str, url: str, body: str,
    lm_url: str | None = None, timeout: float = LM_TIMEOUT,
) -> str:
    """詳細要約用：JSON ではなくテキストをそのまま返す。"""
    prompt = DETAIL_PROMPT_TEMPLATE.format(title=title or "", url=url, body=body)
    return _post_chat(prompt, lm_url, timeout)


# ---- 1タブの要約 ----


def summarize_url(url: str, title: str | None) -> SummaryResult:
    """fetch → 抽出 → LLM、を一通り実行して結果を返す。例外は SummaryResult.error に詰める。"""
    status, html = fetch_body(url)
    if status == -1:
        return SummaryResult(
            summary="アクセス不可（ネットワークエラー）",
            tags=["dead-link"],
            is_dead=True,
        )
    if status >= 400:
        tags = ["dead-link"]
        if status == 404:
            tags.append("404")
        return SummaryResult(
            summary=f"アクセス不可（HTTP {status}）",
            tags=tags,
            is_dead=True,
        )
    body = extract_text(html)
    if not body.strip():
        return SummaryResult(
            summary="本文を抽出できませんでした",
            tags=["empty"],
            is_dead=True,
        )
    try:
        obj = call_llm(title or "", url, body)
    except Exception as e:  # noqa: BLE001 - LLMエラーは握って Result に
        log.exception("LLM call failed for %s", url)
        return SummaryResult(summary="(要約失敗)", tags=[], error=str(e))
    summary = _normalize_summary(str(obj.get("summary", ""))) or "(空)"
    tags_raw = obj.get("tags") or []
    if not isinstance(tags_raw, list):
        tags_raw = []
    tags = [str(t).strip() for t in tags_raw if str(t).strip()][:3]
    return SummaryResult(summary=summary, tags=tags)


# ---- DB操作 ----


async def persist_summary(
    db_path: str, tab_id: int, result: SummaryResult, now: int
) -> None:
    """短い要約結果を tabs.summary に保存し、提案タグを付与（既存タグはスキップ）。"""
    async with get_db(db_path) as conn:
        await conn.execute(
            "UPDATE tabs SET summary = ?, summarized_at = ?, updated_at = ? WHERE id = ?",
            (result.summary, now, now, tab_id),
        )
        for name in result.tags:
            try:
                await add_tab_tag(conn, tab_id, name)
            except ValueError:
                continue
        await conn.commit()


def summarize_url_detail(url: str, title: str | None) -> DetailResult:
    """5-7行の詳細要約。タグ付与は行わない。"""
    status, html = fetch_body(url)
    if status == -1:
        return DetailResult(text="アクセス不可（ネットワークエラー）", is_dead=True)
    if status >= 400:
        return DetailResult(text=f"アクセス不可（HTTP {status}）", is_dead=True)
    body = extract_text(html)
    if not body.strip():
        return DetailResult(text="本文を抽出できませんでした", is_dead=True)
    try:
        text = call_llm_text(title or "", url, body)
    except Exception as e:  # noqa: BLE001
        log.exception("LLM detail call failed for %s", url)
        return DetailResult(text="(詳細要約失敗)", error=str(e))
    return DetailResult(text=_normalize_summary(text) or "(空)")


async def persist_detail_summary(
    db_path: str, tab_id: int, text: str, now: int
) -> None:
    async with get_db(db_path) as conn:
        await conn.execute(
            "UPDATE tabs SET summary_long = ?, summarized_long_at = ?, updated_at = ? "
            "WHERE id = ?",
            (text, now, now, tab_id),
        )
        await conn.commit()


async def summarize_tab(
    db_path: str, tab_id: int, detail: bool = False, now: int | None = None
) -> SummaryResult | DetailResult:
    """1タブを要約。detail=True なら詳細要約 (summary_long) を保存。"""
    async with get_db(db_path) as conn:
        tab = await get_tab(conn, tab_id)
    if tab is None:
        raise ValueError(f"tab {tab_id} not found")
    timestamp = now or int(time.time())
    if detail:
        result = summarize_url_detail(tab.url, tab.title)
        await persist_detail_summary(db_path, tab_id, result.text, timestamp)
        return result
    result = summarize_url(tab.url, tab.title)
    await persist_summary(db_path, tab_id, result, timestamp)
    return result


async def summarize_pending(
    db_path: str = DEFAULT_DB_PATH, max_count: int = 10
) -> list[tuple[int, SummaryResult]]:
    """未要約 (summary IS NULL) のタブを最大 N件 順に要約する。"""
    await init_db(db_path)
    async with get_db(db_path) as conn:
        cur = await conn.execute(
            "SELECT id FROM tabs WHERE summary IS NULL "
            "ORDER BY updated_at DESC LIMIT ?",
            (max_count,),
        )
        rows = await cur.fetchall()
    results: list[tuple[int, SummaryResult]] = []
    for (tab_id,) in rows:
        log.info("summarize tab %d", tab_id)
        result = await summarize_tab(db_path, tab_id)
        results.append((tab_id, result))
    return results


# ---- CLI ----


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="未要約タブを LM Studio で要約する")
    parser.add_argument(
        "--max", type=int, default=10, help="一度に処理する最大タブ数"
    )
    parser.add_argument("--tab", type=int, help="特定の tab_id だけ処理する")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )

    if args.tab:
        result = asyncio.run(summarize_tab(DEFAULT_DB_PATH, args.tab))
        mark = "💀" if result.is_dead else "❌" if result.error else "✓"
        print(f"{mark} tab {args.tab}")
        print(f"  summary: {result.summary[:120]}")
        print(f"  tags   : {result.tags}")
        if result.error:
            print(f"  error  : {result.error}")
    else:
        results = asyncio.run(summarize_pending(DEFAULT_DB_PATH, args.max))
        print(f"処理: {len(results)}件")
        for tab_id, r in results:
            mark = "💀" if r.is_dead else "❌" if r.error else "✓"
            print(f"  {mark} tab {tab_id}: {r.summary[:60]}")


if __name__ == "__main__":
    main()
