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
PROTECTED_NAMES = {"articles", ".git", "data", "inbox"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def manifest(path: Path) -> list[dict[str, object]]:
    """Return a deterministic file manifest for a file or directory."""

    if path.is_file():
        return [{"path": path.name, "size": path.stat().st_size, "sha256": sha256(path)}]
    rows: list[dict[str, object]] = []
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        relative = child.relative_to(path)
        rows.append({"path": str(relative), "size": child.stat().st_size, "sha256": sha256(child)})
    return rows


def candidates(root: Path, older_than: dt.datetime) -> list[Path]:
    root = root.resolve()
    if root.name in PROTECTED_NAMES:
        raise ValueError(f"Refusing to clean protected root: {root}")
    if root in {Path("/"), Path.home()} or (root / ".git").exists():
        raise ValueError(f"Refusing to clean a broad or repository root: {root}")
    if not root.is_dir():
        raise NotADirectoryError(root)
    result: list[Path] = []
    for path in root.iterdir():
        if path.name in PROTECTED_NAMES:
            continue
        modified = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
        if modified < older_than:
            result.append(path)
    return sorted(result)


def backup_candidate(path: Path, root: Path, backup_root: Path) -> Path:
    root = root.resolve()
    backup_root = backup_root.resolve()
    if backup_root == root or root in backup_root.parents:
        raise ValueError("Backup root must be outside the cleanup root")
    target = backup_root / root.name / path.relative_to(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir():
        shutil.copytree(path, target)
    else:
        shutil.copy2(path, target)
    if manifest(path) != manifest(target):
        raise ValueError(f"Backup checksum verification failed for {path}")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Explicit staging or raw-data root to audit")
    parser.add_argument("--older-than-days", type=int, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument("--report", type=Path, default=Path("_article_pipeline/backup_cleanup.jsonl"))
    parser.add_argument(
        "--backup-root",
        type=Path,
        help="External restore location required with --apply; candidates are copied and verified before removal.",
    )
    parser.add_argument("--apply", action="store_true", help="Delete candidates after printing the dry-run report")
    args = parser.parse_args(argv)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.older_than_days)
    items = candidates(args.root, cutoff)
    if args.apply and args.backup_root is None:
        parser.error("--apply requires --backup-root so every deletion has a verified restore copy")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in items:
        row = {"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(), "path": str(path), "apply": args.apply}
        row["manifest"] = manifest(path)
        if args.apply:
            restore_path = backup_candidate(path, args.root, args.backup_root)
            row["restore_path"] = str(restore_path)
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))
    with args.report.open("a", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
