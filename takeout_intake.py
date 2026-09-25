#!/usr/bin/env python3
"""Safely stage an extracted Google Takeout tree without deleting source data."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from activity_pipeline import sha256_file


SKIP_NAMES = {"archive_browser.html"}
SKIP_DIRS = {".git", "__pycache__"}


@dataclass(frozen=True)
class IntakeEntry:
    source: str
    destination: str
    size: int
    sha256: str
    action: str


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _iter_files(source: Path):
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(source).parts
        if any(part in SKIP_DIRS for part in relative_parts) or path.name in SKIP_NAMES:
            continue
        yield path


def copy_takeout_tree(
    source: Path,
    destination: Path,
    *,
    dry_run: bool = True,
    manifest_path: Path | None = None,
) -> list[IntakeEntry]:
    """Copy files idempotently and write a checksum manifest.

    The default is a dry run.  The source is only read; no source file or
    directory is removed, renamed, or modified.
    """

    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir():
        raise NotADirectoryError(source)
    if source == destination or source in destination.parents:
        raise ValueError("Destination must be separate from and outside the source tree")
    manifest = manifest_path or destination / "_article_pipeline" / "intake_manifest.json"
    entries: list[IntakeEntry] = []
    for path in _iter_files(source):
        relative = path.relative_to(source)
        target = destination / relative
        digest = sha256_file(path)
        action = "unchanged" if target.is_file() and sha256_file(target) == digest else "copy"
        entry = IntakeEntry(str(relative), str(relative), path.stat().st_size, digest, action)
        entries.append(entry)
        if not dry_run and action == "copy":
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".tmp")
            shutil.copy2(path, temporary)
            os.replace(temporary, target)

    if not dry_run:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "created_at": utc_now(),
            "source": str(source),
            "destination": str(destination),
            "dry_run": False,
            "files": [entry.__dict__ for entry in entries],
        }
        manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return entries


def discover_input_roots(root: Path) -> list[Path]:
    """Return a stable set of roots containing extracted MyActivity records."""

    root = root.resolve()
    if root.is_file():
        return [root] if root.name == "MyActivity.json" else []
    records = sorted(path for path in root.rglob("MyActivity.json") if path.is_file())
    return [root] if records else []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--apply", action="store_true", help="Copy files; without this flag only plan the copy.")
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args(argv)
    entries = copy_takeout_tree(
        args.source,
        args.destination,
        dry_run=not args.apply,
        manifest_path=args.manifest,
    )
    print(json.dumps({"dry_run": not args.apply, "files": [entry.__dict__ for entry in entries]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

