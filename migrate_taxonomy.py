#!/usr/bin/env python3
"""Migrate published articles into the controlled broad taxonomy."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
from pathlib import Path

from taxonomy import classify, parse_tags, replace_tags


FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)


def split_document(text: str) -> tuple[str, str]:
    """Read normal YAML frontmatter and the small legacy header variant."""
    match = FRONTMATTER_RE.match(text)
    if match:
        return match.group(1), text[match.end():]
    lines = text.splitlines()
    header: list[str] = []
    index = 0
    in_tags = False
    while index < len(lines):
        line = lines[index]
        if re.match(r"^(Title|Date|Tags|Source|Split_From_Line):", line):
            header.append(line)
            in_tags = line.startswith("Tags:")
        elif in_tags and re.match(r"^[ \t]+- ", line):
            header.append(line)
        else:
            break
        index += 1
    if header and any(header_line.startswith(prefix) for header_line in header for prefix in ("Title:", "Date:", "Tags:")):
        return "\n".join(header), "\n".join(lines[index:]).lstrip()
    raise ValueError("article has no readable frontmatter")


def frontmatter_title(frontmatter: str, fallback: str) -> str:
    match = re.search(r"^Title:\s*(?:\"([^\"]*)\"|'([^']*)'|(.*))$", frontmatter, re.M)
    if not match:
        return fallback
    return next((part.strip() for part in match.groups() if part is not None), fallback)


def update_article(text: str, category_label: str, provenance: list[str]) -> str:
    frontmatter, body = split_document(text)
    frontmatter = replace_tags(frontmatter, category_label)
    category_line = f'Category: "{category_label}"'
    if re.search(r"^Category:\s*", frontmatter, re.M):
        frontmatter = re.sub(r"^Category:\s*.*$", category_line, frontmatter, count=1, flags=re.M)
    else:
        frontmatter = frontmatter.rstrip() + "\n" + category_line
    if provenance:
        values = "\n".join(f"  - {item}" for item in sorted(set(provenance)))
        provenance_block = f"Source_Products:\n{values}"
        if re.search(r"^Source_Products:\s*\n(?:^[ \t]+- .*\n?)+", frontmatter, re.M):
            frontmatter = re.sub(r"^Source_Products:\s*\n(?:^[ \t]+- .*\n?)+", provenance_block, frontmatter, count=1, flags=re.M)
        elif re.search(r"^Source_Products:\s*", frontmatter, re.M):
            frontmatter = re.sub(r"^Source_Products:\s*.*$", provenance_block, frontmatter, count=1, flags=re.M)
        else:
            frontmatter = frontmatter.rstrip() + "\n" + provenance_block
    return f"---\n{frontmatter.rstrip()}\n---\n{body.lstrip()}"


def migrate(source_dir: Path, *, dry_run: bool, manifest_path: Path) -> dict[str, object]:
    files = sorted(
        path for path in source_dir.rglob("*.md")
        if "_article_pipeline" not in path.parts and path.name != "article_surgeon_progress_tree.md"
    )
    rows: list[dict[str, object]] = []
    planned: dict[Path, Path] = {}
    for path in files:
        text = path.read_text(encoding="utf-8")
        try:
            fm, body = split_document(text)
        except ValueError:
            rows.append({"source": str(path), "status": "error", "reason": "missing frontmatter"})
            continue
        title = frontmatter_title(fm, path.stem)
        tags = parse_tags(fm)
        category, confidence, reason = classify(title, tags, path.parent.name, body)
        destination = source_dir / category.slug / path.name
        if destination.exists() and destination != path and destination not in planned:
            raise RuntimeError(f"destination already exists: {destination}")
        if destination in planned.values() and planned.get(destination) != path:
            raise RuntimeError(f"destination collision: {destination}")
        planned[path] = destination
        provenance = [tag for tag in tags if tag.casefold() in {"search", "ai_mode", "ai mode", "gemini_apps", "gemini apps"}]
        output = update_article(text, category.label, provenance)
        rows.append({
            "source": str(path.relative_to(source_dir)),
            "destination": str(destination.relative_to(source_dir)),
            "title": title,
            "old_tags": tags,
            "category": category.label,
            "category_slug": category.slug,
            "confidence": confidence,
            "reason": reason,
            "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "changed": output != text or destination != path,
            "status": "planned" if dry_run else "migrated",
        })
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            output_path = destination.with_suffix(destination.suffix + ".taxonomy-tmp")
            output_path.write_text(output, encoding="utf-8")
            output_path.replace(destination)
            if destination != path and path.exists():
                path.unlink()

    if not dry_run:
        for directory in sorted((path for path in source_dir.iterdir() if path.is_dir()), reverse=True):
            if directory.name == "_article_pipeline":
                continue
            try:
                directory.rmdir()
            except OSError:
                pass

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "".join(json.dumps({"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(), **row}, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return {"articles": len(files), "rows": rows, "dry_run": dry_run}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("articles"))
    parser.add_argument("--manifest", type=Path, default=Path("articles/_article_pipeline/taxonomy_migration.jsonl"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    report = migrate(args.source, dry_run=args.dry_run, manifest_path=args.manifest)
    print(json.dumps({"articles": report["articles"], "dry_run": report["dry_run"], "manifest": str(args.manifest)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
