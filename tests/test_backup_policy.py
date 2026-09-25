import datetime as dt
from pathlib import Path

import pytest

from backup_policy import backup_candidate, candidates


def test_cleanup_refuses_raw_and_repository_roots(tmp_path: Path) -> None:
    raw = tmp_path / "data"
    raw.mkdir()
    with pytest.raises(ValueError, match="protected root"):
        candidates(raw, dt.datetime.now(dt.timezone.utc))

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(ValueError, match="repository root"):
        candidates(repo, dt.datetime.now(dt.timezone.utc))


def test_backup_candidate_verifies_directory_manifest(tmp_path: Path) -> None:
    root = tmp_path / "staging"
    candidate = root / "old"
    candidate.mkdir(parents=True)
    (candidate / "artifact.json").write_text("{}", encoding="utf-8")
    restore = tmp_path / "external-backup"
    target = backup_candidate(candidate, root, restore)
    assert target == restore / "staging" / "old"
    assert (target / "artifact.json").read_text(encoding="utf-8") == "{}"
