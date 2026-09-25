import json
from pathlib import Path

from takeout_intake import copy_takeout_tree, discover_input_roots


def test_copy_is_dry_run_by_default_and_skips_browser_indexes(tmp_path: Path) -> None:
    source = tmp_path / "data"
    destination = tmp_path / "inbox"
    record = source / "Takeout-20260913" / "Takeout" / "My Activity" / "AI Mode" / "MyActivity.json"
    record.parent.mkdir(parents=True)
    record.write_text("[]", encoding="utf-8")
    (source / "Takeout-20260913" / "archive_browser.html").write_text("index", encoding="utf-8")
    (source / "Takeout-20260913" / ".git").mkdir()
    (source / "Takeout-20260913" / ".git" / "config").write_text("secret", encoding="utf-8")

    planned = copy_takeout_tree(source, destination)
    assert planned[0].action == "copy"
    assert not destination.exists()
    assert discover_input_roots(source) == [source.resolve()]

    applied = copy_takeout_tree(source, destination, dry_run=False)
    assert applied[0].sha256
    assert (destination / record.relative_to(source)).read_text(encoding="utf-8") == "[]"
    assert not (destination / "Takeout-20260913" / "archive_browser.html").exists()
    manifest = json.loads((destination / "_article_pipeline" / "intake_manifest.json").read_text())
    assert manifest["files"][0]["sha256"] == applied[0].sha256

