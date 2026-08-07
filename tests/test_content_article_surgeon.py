from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

import content_article_surgeon as surgeon
from content_article_surgeon import deterministic_conversation_cleanup


def test_deterministic_cleanup_removes_markers_but_preserves_content() -> None:
    source = "Your prompt:\nHow do I inspect DAG errors?\nSearch's response:\nRun `airflow dags list-import-errors`.\n"
    cleaned, removed = deterministic_conversation_cleanup(source)
    assert removed == 2
    assert "Your prompt:" not in cleaned
    assert "Search's response:" not in cleaned
    assert "airflow dags list-import-errors" in cleaned


def test_llm_audit_never_receives_or_removes_frontmatter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    article = tmp_path / "article.md"
    article.write_text(
        "---\nTitle: Keep this title\nDate: 2026-08-05\nTags:\n  - Test\n---\n"
        "Remove this body line.\nKeep this useful body line.\n",
        encoding="utf-8",
    )

    async def fake_audit(client, config, text):
        assert "Title:" not in text
        assert "Date:" not in text
        return {1}, []

    monkeypatch.setattr(surgeon, "llm_conversation_audit", fake_audit)
    monkeypatch.setattr(surgeon, "needs_conversation_audit", lambda text: True)
    config = surgeon.PipelineConfig(
        source_dir=tmp_path,
        articles=None,
        dry_run=False,
        recursive=True,
        openrouter_base_url="https://example.test/v1",
        openrouter_model="openrouter/free",
        openrouter_key="",
        local_base_url="http://local/v1",
        local_model="/models/gemma.gguf",
        split_evaluator="none",
        conversation_auditor="local",
        min_split_confidence=0.9,
        min_segment_chars=100,
        max_heading_map_chars=1000,
        max_audit_window_lines=100,
        max_audit_window_chars=1000,
        temperature=0.0,
        timeout=10.0,
        force=True,
    )

    async def run() -> None:
        async with httpx.AsyncClient() as client:
            await surgeon.process_file(client, config, article)

    asyncio.run(run())
    updated = article.read_text(encoding="utf-8")
    assert "Title: Keep this title" in updated
    assert "Date: 2026-08-05" in updated
    assert "Remove this body line." not in updated
    assert "Keep this useful body line." in updated


def test_model_gates_require_ambiguous_transcript_or_applicable_split() -> None:
    assert surgeon.needs_conversation_audit("Short ordinary prose") is False
    assert surgeon.needs_conversation_audit("You said:\n" + "useful details " * 20) is True
    assert surgeon.needs_topic_split("# One\nShort\n# Two\nShort", min_segment_chars=100) is False
    assert surgeon.needs_topic_split(
        "# One\n" + ("first topic " * 80) + "\n# Two\n" + ("second topic " * 80),
        min_segment_chars=100,
    ) is True
