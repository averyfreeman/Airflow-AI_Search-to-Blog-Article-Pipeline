from __future__ import annotations

import json
from pathlib import Path

import httpx

from migrate_taxonomy import migrate
from research import research_article
from taxonomy import CATEGORY_BY_SLUG, classify
from validate_content import validate_tree


def test_classification_is_allowlisted_and_broad() -> None:
    category, confidence, reason = classify(
        "How to configure a Samba Active Directory domain controller",
        ["Search", "AI_Mode"],
        "it_networking",
        "The article explains LDAP, Kerberos, and domain authentication.",
    )
    assert category.slug == "security-identity"
    assert category.slug in CATEGORY_BY_SLUG
    assert confidence > 0.5
    assert reason


def test_migration_writes_one_category_and_moves_file(tmp_path: Path) -> None:
    source = tmp_path / "articles"
    old = source / "narrow_subject"
    old.mkdir(parents=True)
    article = old / "example.md"
    article.write_text(
        '---\nTitle: "Linux package installation"\nDate: "2026-01-01"\nTags:\n  - Search\n  - AI_Mode\n---\n'
        "A useful article about Ubuntu and package management with practical commands. "
        "It explains when to refresh metadata, how to inspect a failed installation, and what to verify afterward.\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "migration.jsonl"
    report = migrate(source, dry_run=False, manifest_path=manifest)
    assert report["articles"] == 1
    destination = source / "linux-unix" / "example.md"
    assert destination.is_file()
    assert not old.exists()
    text = destination.read_text(encoding="utf-8")
    assert 'Category: "Linux and Unix"' in text
    assert "  - Linux and Unix" in text
    assert "Source_Products:" in text
    assert not validate_tree(source)["errors"]
    assert json.loads(manifest.read_text().splitlines()[0])["destination"] == "linux-unix/example.md"


def test_research_adapter_is_auditable_without_real_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "html.duckduckgo.com" in str(request.url):
            return httpx.Response(
                200,
                text='<div class="result"><a class="result__a" href="https://docs.example.test/page">Official docs</a>'
                '<a class="result__snippet">An authoritative explanation.</a></div>',
                request=request,
            )
        return httpx.Response(200, text="<html><main>Canonical documentation and versioned facts.</main></html>", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    result = research_article("Package installation", {"topic": "Linux"}, client=client)
    client.close()
    assert result["sources"]
    assert result["sources"][0]["url"] == "https://docs.example.test/page"
    assert "Canonical documentation" in result["sources"][0]["extracted_text"]
