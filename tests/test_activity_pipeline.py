from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

import pytest

import activity_pipeline as pipeline


FIXTURES = Path(__file__).parent / "fixtures"


def test_parses_ai_mode_and_deduplicates_output_paths(tmp_path: Path) -> None:
    source = FIXTURES / "ai_mode" / "MyActivity.json"
    records = list(pipeline.iter_activity_records(source))
    assert len(records) == 2
    assert records[0].source_product == "ai_mode"
    assert records[0].occurred_at == "2026-07-15T17:30:00Z"
    assert "airflow dags list-import-errors" in records[0].markdown_body

    first = pipeline.process_input(source, tmp_path)
    second = pipeline.process_input(source, tmp_path)
    assert first == second
    assert len(first) == 1
    assert len(list(tmp_path.glob("*.md"))) == 1
    rendered = first[0].read_text(encoding="utf-8")
    assert "Category: \"Software Development\"" in rendered
    assert rendered.count("  - Software Development") == 1
    assert "  - Search" not in rendered
    assert "  - AI_Mode" not in rendered


def test_parses_gemini_attachments_subtitles_and_missing_optional_fields() -> None:
    source = FIXTURES / "gemini_apps" / "MyActivity.json"
    records = list(pipeline.iter_activity_records(source))
    assert len(records) == 2
    assert records[0].source_product == "gemini_apps"
    assert records[0].attachments == ("sample.zip", "notes.txt")
    assert records[0].subtitles[0]["url"].startswith("https://docs.python.org/")
    assert records[1].markdown_body == ""
    assert records[1].occurred_at == "2026-07-17T01:02:03Z"


def test_safe_zip_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../escape.txt", "nope")
    with pytest.raises(ValueError, match="Unsafe archive member"):
        pipeline.validate_archive(archive)


def test_safe_tar_rejects_symlinks(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.tar"
    with tarfile.open(archive, "w") as handle:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        handle.addfile(info)
    with pytest.raises(ValueError, match="Unsafe TAR member type"):
        pipeline.validate_archive(archive)


def test_corrupt_archive_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "broken.zip"
    archive.write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="Corrupt ZIP"):
        pipeline.validate_archive(archive)


def test_archive_extract_and_process_round_trip(tmp_path: Path) -> None:
    archive = tmp_path / "takeout.zip"
    payload = (FIXTURES / "gemini_apps" / "MyActivity.json").read_bytes()
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("Takeout/My Activity/Gemini Apps/MyActivity.json", payload)
    output = tmp_path / "out"
    paths = pipeline.process_input(archive, output)
    assert len(paths) == 2
    assert all(path.is_file() for path in paths)


def test_full_path_model_id_and_thought_channel_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    full_id = "/models/gemma-4-26B-A4B-it-QAT-Q4_0.gguf"

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": [{"id": full_id}]}

    monkeypatch.setattr(pipeline.httpx, "get", lambda *args, **kwargs: Response())
    assert pipeline.discover_single_model("http://localhost:11400/v1") == full_id
    raw = "<|channel>thought hidden reasoning<channel|>\n# Final answer"
    assert pipeline.strip_thought_channels(raw) == "# Final answer"
