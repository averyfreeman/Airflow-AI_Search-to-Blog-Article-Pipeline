from pathlib import Path

from validate_content import validate_tree


def test_validation_requires_exactly_one_category(tmp_path: Path) -> None:
    article_dir = tmp_path / "software-development"
    article_dir.mkdir()
    article = article_dir / "article.md"
    article.write_text(
        "---\nCategory: Software Development\nCategory: AI\nTags:\n  - Software Development\n---\n"
        + "A sufficiently long article body. " * 10,
        encoding="utf-8",
    )
    report = validate_tree(tmp_path)
    assert any("exactly one Category" in error for error in report["errors"])

