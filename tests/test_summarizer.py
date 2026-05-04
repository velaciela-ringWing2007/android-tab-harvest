"""summarizer.py のユニットテスト（HTTP/LLMはmonkeypatchでモック）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import summarizer
from db import get_db, get_tab, init_db, list_tab_tags, upsert_tab


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "tabs.db")


class TestParseLlmResponse:
    def test_pure_json(self) -> None:
        obj = summarizer.parse_llm_response('{"summary":"x","tags":["a","b"]}')
        assert obj["summary"] == "x"
        assert obj["tags"] == ["a", "b"]

    def test_codeblock_wrapped(self) -> None:
        text = '```json\n{"summary":"x","tags":["a"]}\n```'
        assert summarizer.parse_llm_response(text)["summary"] == "x"

    def test_codeblock_no_lang(self) -> None:
        text = '```\n{"summary":"x"}\n```'
        assert summarizer.parse_llm_response(text)["summary"] == "x"

    def test_with_surrounding_text(self) -> None:
        text = '結果はこちらです: {"summary":"x","tags":[]} です'
        assert summarizer.parse_llm_response(text)["summary"] == "x"

    def test_invalid_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            summarizer.parse_llm_response("これはJSONではない")


class TestSummarizeUrl:
    def test_404_marks_dead_link(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(summarizer, "fetch_body", lambda url, timeout=15.0: (404, ""))
        result = summarizer.summarize_url("https://x.example/", "T")
        assert result.is_dead is True
        assert "404" in result.tags
        assert "dead-link" in result.tags
        assert "404" in result.summary

    def test_410_marks_dead_link_without_404_tag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(summarizer, "fetch_body", lambda url, timeout=15.0: (410, ""))
        result = summarizer.summarize_url("https://x.example/", "T")
        assert result.is_dead is True
        assert result.tags == ["dead-link"]

    def test_network_error_marks_dead_link(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(summarizer, "fetch_body", lambda url, timeout=15.0: (-1, ""))
        result = summarizer.summarize_url("https://x.example/", "T")
        assert result.is_dead is True
        assert "dead-link" in result.tags

    def test_empty_body_marks_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(summarizer, "fetch_body", lambda url, timeout=15.0: (200, "<html></html>"))
        monkeypatch.setattr(summarizer, "extract_text", lambda html: "")
        result = summarizer.summarize_url("https://x.example/", "T")
        assert result.is_dead is True
        assert "empty" in result.tags

    def test_happy_path_returns_summary_and_tags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(summarizer, "fetch_body", lambda url, timeout=15.0: (200, "<p>本文</p>"))
        monkeypatch.setattr(summarizer, "extract_text", lambda html: "ここに本文")

        def fake_llm(title: str, url: str, body: str, **kwargs: Any) -> dict[str, Any]:
            return {"summary": "3行要約", "tags": ["python", "ai", "extra", "ignored"]}

        monkeypatch.setattr(summarizer, "call_llm", fake_llm)
        result = summarizer.summarize_url("https://x.example/", "T")
        assert result.is_dead is False
        assert result.summary == "3行要約"
        assert result.tags == ["python", "ai", "extra"]  # 最大3個

    def test_llm_error_recorded_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(summarizer, "fetch_body", lambda url, timeout=15.0: (200, "x"))
        monkeypatch.setattr(summarizer, "extract_text", lambda html: "本文あり")

        def boom(title: str, url: str, body: str, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("LM Studio offline")

        monkeypatch.setattr(summarizer, "call_llm", boom)
        result = summarizer.summarize_url("https://x.example/", "T")
        assert result.error is not None
        assert "LM Studio offline" in result.error
        assert result.is_dead is False


class TestPersistAndPending:
    async def test_persist_summary_writes_db_and_tags(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await init_db(db_path)
        async with get_db(db_path) as conn:
            tid = await upsert_tab(conn, "https://x.example/", "h_x", "T", 1700000000)
            await conn.commit()

        result = summarizer.SummaryResult(summary="ABC", tags=["python", "memo"])
        await summarizer.persist_summary(db_path, tid, result, 1700001234)

        async with get_db(db_path) as conn:
            tab = await get_tab(conn, tid)
            tags = await list_tab_tags(conn, tid)
        assert tab.summary == "ABC"
        assert tab.summarized_at == 1700001234
        assert sorted(t.name for t in tags) == ["memo", "python"]

    async def test_summarize_pending_processes_only_unsummarized(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await init_db(db_path)
        async with get_db(db_path) as conn:
            t1 = await upsert_tab(conn, "https://a/", "h_a", "A", 1700000000)
            t2 = await upsert_tab(conn, "https://b/", "h_b", "B", 1700000000)
            # t1 だけ既に要約済み
            await conn.execute(
                "UPDATE tabs SET summary = '済', summarized_at = 1 WHERE id = ?",
                (t1,),
            )
            await conn.commit()

        # 全部 fake で固定の結果を返す
        monkeypatch.setattr(
            summarizer, "summarize_url",
            lambda url, title: summarizer.SummaryResult(summary="OK", tags=["x"]),
        )
        results = await summarizer.summarize_pending(db_path, max_count=10)
        assert [tid for tid, _ in results] == [t2]

    async def test_summarize_pending_respects_max(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await init_db(db_path)
        async with get_db(db_path) as conn:
            for i in range(5):
                await upsert_tab(
                    conn, f"https://x{i}/", f"h{i}", f"T{i}", 1700000000 + i
                )
            await conn.commit()

        monkeypatch.setattr(
            summarizer, "summarize_url",
            lambda url, title: summarizer.SummaryResult(summary="OK", tags=[]),
        )
        results = await summarizer.summarize_pending(db_path, max_count=2)
        assert len(results) == 2
