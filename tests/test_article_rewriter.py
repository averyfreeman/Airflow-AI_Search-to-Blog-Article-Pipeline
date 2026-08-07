from __future__ import annotations

import json
from pathlib import Path

import pytest

from activity_pipeline import frontmatter_value
from article_rewriter import protect_markdown, restore_markdown, rewrite_file


class EchoRewriteClient:
    base_url = "http://local/v1"
    model = "/models/gemma-4-26B-A4B-it-QAT-Q4_0.gguf"
    context = 65_792

    def rewrite(self, chunk: str, brief: dict, *, index: int, total: int) -> str:
        assert brief["resolution"] == "Use the import-error command"
        return chunk.replace("Your prompt:\n", "").replace("Search's response:\n", "")


def create_scored_article(root: Path) -> Path:
    article = root / "article.md"
    body = """Your prompt:
Why does the DAG fail?

Search's response:
Run the diagnostic command, inspect the import traceback, and fix the invalid Python import before changing scheduling behavior.

```bash
airflow dags list-import-errors
```

Read the [Airflow documentation](https://airflow.apache.org/docs/apache-airflow/stable/) and preserve uncertainty about environment-specific paths.
"""
    article.write_text(
        "---\nTitle: Airflow import debugging\nDate: 2026-07-15_17_30\nTags:\n  - Airflow\n"
        "Score_Disposition: rewrite\nScore_Record: \"_article_pipeline/scores/article.score.json\"\n---\n"
        + body,
        encoding="utf-8",
    )
    score = root / "_article_pipeline" / "scores" / "article.score.json"
    score.parent.mkdir(parents=True)
    score.write_text(
        json.dumps(
            {
                "disposition": "rewrite",
                "brief": {
                    "resolution": "Use the import-error command",
                    "facts_and_code_to_preserve": ["airflow dags list-import-errors"],
                },
            }
        ),
        encoding="utf-8",
    )
    return article


def test_rewrite_preserves_frontmatter_links_code_and_commands(tmp_path: Path) -> None:
    article = create_scored_article(tmp_path)
    result = rewrite_file(article, source_dir=tmp_path, client=EchoRewriteClient())
    updated = article.read_text(encoding="utf-8")
    assert frontmatter_value(updated, "Title") == "Airflow import debugging"
    assert frontmatter_value(updated, "Rewrite_Model") == "gemma-4-26B-A4B-it-QAT-Q4_0.gguf"
    assert "https://airflow.apache.org/docs/apache-airflow/stable/" in updated
    assert "```bash\nairflow dags list-import-errors\n```" in updated
    assert "Your prompt:" not in updated
    assert Path(result["backup"]).is_file()


def test_missing_protected_token_fails_integrity_check() -> None:
    protected_text, protected = protect_markdown("See [docs](https://example.test) and:\n```sh\nrun it\n```")
    first_token = next(iter(protected))
    with pytest.raises(ValueError, match="omitted"):
        restore_markdown(protected_text.replace(first_token, ""), protected)
