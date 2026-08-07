#!/usr/bin/env python3
"""Audit or clean old local pipeline artifacts without touching published articles."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
from pathlib import Path


DEFAULT_RETENTION_DAYS = 30
PROTECTED_NAMES = {"articles", ".git"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def candidates(root: Path, older_than: dt.datetime) -> list[Path]:
    root = root.resolve()
    if root.name in PROTECTED_NAMES:
        raise ValueError(f"Refusing to clean protected root: {root}")
    result: list[Path] = []
    for path in root.iterdir():
        if path.name in PROTECTED_NAMES:
            continue
        modified = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
        if modified < older_than:
            result.append(path)
    return sorted(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Explicit staging or raw-data root to audit")
    parser.add_argument("--older-than-days", type=int, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument("--report", type=Path, default=Path("_article_pipeline/backup_cleanup.jsonl"))
    parser.add_argument("--apply", action="store_true", help="Delete candidates after printing the dry-run report")
    args = parser.parse_args(argv)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.older_than_days)
    items = candidates(args.root, cutoff)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in items:
        row = {"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(), "path": str(path), "apply": args.apply}
        if path.is_file():
            row["sha256"] = sha256(path)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))
        if args.apply:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    with args.report.open("a", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
