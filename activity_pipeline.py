#!/usr/bin/env python3
"""Normalize Google Takeout My Activity exports into idempotent Markdown.

The worker accepts a Takeout archive, an extracted directory, or a single
``MyActivity.json`` file. It deliberately keeps extraction and ordinary
HTML-to-Markdown conversion deterministic. Optional image descriptions use the
configured local llama.cpp vision endpoint.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as dt
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import tarfile
import tempfile
import zipfile
import fcntl
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Literal

import httpx
from markdownify import markdownify as html_to_markdown
from taxonomy import classify


SourceProduct = Literal["ai_mode", "gemini_apps"]
ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
DEFAULT_LOCAL_BASE_URL = os.environ.get("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11400/v1")
FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)


@dataclasses.dataclass(frozen=True)
class ActivityRecord:
    source_product: SourceProduct
    source_record_id: str
    occurred_at: str
    title: str
    products: tuple[str, ...]
    activity_controls: tuple[str, ...]
    markdown_body: str
    attachments: tuple[str, ...]
    subtitles: tuple[dict[str, str], ...]
    source_path: str
    content_hash: str


@dataclasses.dataclass(frozen=True)
class ArchiveValidation:
    path: Path
    sha256: str
    members: tuple[str, ...]
    kind: str


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def strip_thought_channels(text: str) -> str:
    """Remove llama.cpp/Gemma preserved thought-channel wrappers.

    Current llama.cpp builds may return ``<|channel>thought ... <channel|>``
    inside ``message.content`` when reasoning preservation is enabled.
    """

    cleaned = re.sub(r"<\|channel>thought\s*.*?<channel\|>", "", text or "", flags=re.S)
    cleaned = re.sub(r"^```(?:markdown|md|text)?\s*", "", cleaned.strip(), flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    return cleaned


def discover_single_model(base_url: str, timeout: float = 10.0) -> str:
    response = httpx.get(base_url.rstrip("/") + "/models", timeout=timeout)
    response.raise_for_status()
    model_ids = [str(item["id"]) for item in response.json().get("data", []) if item.get("id")]
    configured = os.environ.get("LOCAL_LLM_MODEL", "").strip()
    if configured:
        if configured not in model_ids:
            raise RuntimeError(f"LOCAL_LLM_MODEL is not served: {configured}")
        return configured
    if len(model_ids) != 1:
        raise RuntimeError(
            f"Expected exactly one local model, found {len(model_ids)}. Set LOCAL_LLM_MODEL explicitly."
        )
    return model_ids[0]


def _archive_kind(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith((".tar.gz", ".tgz")):
        return "tar.gz"
    if name.endswith(".tar"):
        return "tar"
    raise ValueError(f"Unsupported archive type: {path}")


def _validate_member_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"Unsafe archive member path: {name!r}")
    if not pure.parts or pure.parts[0] in {"", "."}:
        raise ValueError(f"Invalid archive member path: {name!r}")
    return pure.as_posix()


def validate_archive(path: Path) -> ArchiveValidation:
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    kind = _archive_kind(path)
    members: list[str] = []
    if kind == "zip":
        if not zipfile.is_zipfile(path):
            raise ValueError(f"Corrupt ZIP archive: {path}")
        with zipfile.ZipFile(path) as archive:
            bad_member = archive.testzip()
            if bad_member:
                raise ValueError(f"ZIP CRC failure: {bad_member}")
            for info in archive.infolist():
                name = _validate_member_name(info.filename)
                unix_mode = (info.external_attr >> 16) & 0o170000
                if unix_mode == 0o120000:
                    raise ValueError(f"Archive symlink is not allowed: {name}")
                members.append(name)
    else:
        try:
            with tarfile.open(path, "r:*") as archive:
                for member in archive.getmembers():
                    name = _validate_member_name(member.name)
                    if member.issym() or member.islnk() or member.isdev():
                        raise ValueError(f"Unsafe TAR member type: {name}")
                    members.append(name)
        except tarfile.TarError as exc:
            raise ValueError(f"Corrupt TAR archive: {path}") from exc
    return ArchiveValidation(path=path, sha256=sha256_file(path), members=tuple(members), kind=kind)


def _safe_target(root: Path, member_name: str) -> Path:
    root = root.resolve()
    target = (root / member_name).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"Archive member escapes extraction root: {member_name!r}")
    return target


def extract_archive(validation: ArchiveValidation, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        return destination
    if validation.kind == "zip":
        with zipfile.ZipFile(validation.path) as archive:
            for info in archive.infolist():
                name = _validate_member_name(info.filename)
                target = _safe_target(destination, name)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    else:
        with tarfile.open(validation.path, "r:*") as archive:
            for member in archive.getmembers():
                name = _validate_member_name(member.name)
                target = _safe_target(destination, name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"Could not read TAR member: {name}")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    return destination


def discover_activity_jsons(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.name.lower().endswith(".json") else []
    return sorted(path for path in root.rglob("MyActivity.json") if path.is_file())


def normalize_timestamp(raw: str) -> str:
    if not raw:
        raise ValueError("Activity record is missing a timestamp")
    candidate = raw.strip().replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def infer_source_product(path: Path, records: list[dict[str, Any]]) -> SourceProduct:
    path_hint = " ".join(part.lower() for part in path.parts)
    if "gemini apps" in path_hint:
        return "gemini_apps"
    if "ai mode" in path_hint:
        return "ai_mode"
    titles = [str(item.get("title", "")) for item in records[:100]]
    if any(title.startswith("Prompted") for title in titles):
        return "gemini_apps"
    if any(title.startswith("Searched for") for title in titles):
        return "ai_mode"
    raise ValueError(f"Could not infer My Activity product for {path}")


def _clean_title(raw_title: str, source_product: SourceProduct) -> str:
    prefix = r"^Prompted\s+" if source_product == "gemini_apps" else r"^Searched for\s+"
    return re.sub(prefix, "", raw_title, flags=re.I).strip()


def _safe_html(item: dict[str, Any]) -> str:
    blocks = item.get("safeHtmlItem") or []
    if not isinstance(blocks, list):
        return ""
    return "\n".join(str(block.get("html", "")) for block in blocks if isinstance(block, dict)).strip()


def deterministic_html_to_markdown(html: str) -> str:
    if not html:
        return ""
    markdown = html_to_markdown(html, heading_style="ATX", bullets="-")
    markdown = re.sub(r"\n{4,}", "\n\n\n", markdown)
    return markdown.strip()


def _attachments(item: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    image_file = item.get("imageFile")
    if isinstance(image_file, str) and image_file.strip():
        values.append(image_file.strip())
    attached = item.get("attachedFiles") or []
    if isinstance(attached, list):
        for value in attached:
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
            elif isinstance(value, dict):
                candidate = value.get("fileName") or value.get("name") or value.get("path")
                if isinstance(candidate, str) and candidate.strip():
                    values.append(candidate.strip())
    return tuple(dict.fromkeys(values))


def _subtitles(item: dict[str, Any]) -> tuple[dict[str, str], ...]:
    result: list[dict[str, str]] = []
    for subtitle in item.get("subtitles") or []:
        if not isinstance(subtitle, dict):
            continue
        url = str(subtitle.get("url", "")).strip()
        if url:
            result.append({"name": str(subtitle.get("name", "External link")).strip(), "url": url})
    return tuple(result)


def iter_activity_records(path: Path) -> Iterator[ActivityRecord]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"Expected a JSON array in {path}")
    items = [item for item in raw if isinstance(item, dict)]
    source_product = infer_source_product(path, items)
    required_prefix = "Prompted" if source_product == "gemini_apps" else "Searched for"
    for item in items:
        raw_title = str(item.get("title", ""))
        if not raw_title.startswith(required_prefix):
            continue
        timestamp = normalize_timestamp(str(item.get("time", "")))
        title = _clean_title(raw_title, source_product) or "Untitled activity"
        html = _safe_html(item)
        markdown = deterministic_html_to_markdown(html)
        normalized_payload = {
            "source_product": source_product,
            "time": timestamp,
            "title": title,
            "html": html,
            "attachments": _attachments(item),
            "subtitles": _subtitles(item),
        }
        record_id = sha256_bytes(canonical_json(normalized_payload))
        yield ActivityRecord(
            source_product=source_product,
            source_record_id=record_id,
            occurred_at=timestamp,
            title=title,
            products=tuple(str(value) for value in item.get("products") or []),
            activity_controls=tuple(str(value) for value in item.get("activityControls") or []),
            markdown_body=markdown,
            attachments=_attachments(item),
            subtitles=_subtitles(item),
            source_path=str(path),
            content_hash=sha256_bytes(markdown.encode("utf-8")),
        )


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def split_frontmatter(text: str) -> tuple[str, str]:
    match = FRONT_MATTER_RE.match(text)
    if not match:
        return "", text
    return match.group(1), text[match.end() :]


def frontmatter_value(text: str, key: str) -> str | None:
    frontmatter, _ = split_frontmatter(text)
    for line in frontmatter.splitlines():
        if ":" not in line or line.startswith((" ", "\t")):
            continue
        current_key, value = line.split(":", 1)
        if current_key.strip().lower() == key.lower():
            value = value.strip()
            try:
                decoded = json.loads(value)
                return str(decoded)
            except Exception:
                return value.strip("'\"")
    return None


def update_frontmatter(text: str, values: dict[str, str | float | int | bool]) -> str:
    frontmatter, body = split_frontmatter(text)
    existing_lines = frontmatter.splitlines() if frontmatter else []
    replaced_keys = {key.lower() for key in values}
    kept: list[str] = []
    skip_list = False
    for line in existing_lines:
        if line.startswith((" ", "\t")):
            if not skip_list:
                kept.append(line)
            continue
        skip_list = False
        if ":" in line:
            key = line.split(":", 1)[0].strip().lower()
            if key in replaced_keys:
                skip_list = True
                continue
        kept.append(line)
    for key, value in values.items():
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)):
            rendered = str(value)
        else:
            rendered = yaml_string(str(value))
        kept.append(f"{key}: {rendered}")
    return "---\n" + "\n".join(kept) + "\n---\n" + body.lstrip("\n")


def slugify(value: str, max_length: int = 70) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return (slug[:max_length].strip("_") or "untitled")


def _tags(record: ActivityRecord) -> list[str]:
    values = ["Search", "Gemini_Apps" if record.source_product == "gemini_apps" else "AI_Mode"]
    values.extend(product.replace(" ", "_") for product in record.products)
    return list(dict.fromkeys(tag for tag in values if tag))


def _resolve_attachment(source_json: Path, attachment: str) -> Path | None:
    candidates = [source_json.parent / attachment, source_json.parent / Path(attachment).name]
    stem = Path(attachment).stem
    candidates.extend(source_json.parent / f"{stem}{suffix}" for suffix in ["", ".png", ".jpg", ".jpeg", ".webp"])
    return next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)


def _vision_description(image_path: Path, base_url: str, timeout: float) -> str:
    model = discover_single_model(base_url, timeout=min(timeout, 10.0))
    mime = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    response = httpx.post(
        base_url.rstrip("/") + "/chat/completions",
        json={
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Describe only information in this image that materially helps a technical article. "
                                "If it adds no meaningful context, return NOT_USEFUL."
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                    ],
                }
            ],
            "temperature": 0.0,
            "max_tokens": 500,
            "chat_template_kwargs": {"enable_thinking": False},
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return strip_thought_channels(response.json()["choices"][0]["message"]["content"])


def render_markdown(
    record: ActivityRecord,
    *,
    source_json: Path,
    describe_images: bool = False,
    local_base_url: str = DEFAULT_LOCAL_BASE_URL,
    timeout: float = 180.0,
) -> str:
    date_value = dt.datetime.fromisoformat(record.occurred_at.replace("Z", "+00:00")).strftime("%Y-%m-%d_%H_%M")
    category, _, _ = classify(record.title, _tags(record), "", record.markdown_body)
    lines = [
        "---",
        f"Title: {yaml_string(record.title)}",
        f"Date: {yaml_string(date_value)}",
        f"Category: {yaml_string(category.label)}",
        "Tags:",
        f"  - {category.label}",
        "Source: google_takeout",
        f"Source_Product: {record.source_product}",
        f"Source_Record_ID: {record.source_record_id}",
        f"Content_Hash: {record.content_hash}",
        "---",
        "",
    ]
    body: list[str] = []
    for attachment in record.attachments:
        resolved = _resolve_attachment(source_json, attachment)
        target = resolved.name if resolved else attachment
        suffix = Path(target).suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            body.append(f"![Attached image]({target})")
            if describe_images and resolved and resolved.stat().st_size >= 4096:
                description = _vision_description(resolved, local_base_url, timeout)
                if description and description != "NOT_USEFUL":
                    body.extend(["", f"*Image context: {description}*"])
        else:
            body.append(f"[Attached file: {Path(attachment).name}]({target})")
    if body and record.markdown_body:
        body.append("")
    body.append(record.markdown_body or record.title)
    if record.subtitles:
        body.extend(["", "## References", ""])
        body.extend(f"- [{item['name']}]({item['url']})" for item in record.subtitles)
    return "\n".join(lines + body).rstrip() + "\n"


def output_path(record: ActivityRecord, output_dir: Path) -> Path:
    timestamp = dt.datetime.fromisoformat(record.occurred_at.replace("Z", "+00:00")).strftime("%Y-%m-%d_%H_%M")
    return output_dir / f"{timestamp}_{slugify(record.title)}_{record.source_record_id[:10]}.md"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        stream.write(content)
        temporary = Path(stream.name)
    os.replace(temporary, path)


def append_jsonl(path: Path, event: dict[str, Any]) -> None:
    """Append one complete JSONL record while coordinating mapped workers."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def append_manifest(output_dir: Path, event: dict[str, Any]) -> None:
    manifest = output_dir / "_article_pipeline" / "ingest_manifest.jsonl"
    append_jsonl(manifest, {"timestamp": utc_now(), **event})


def process_json(
    json_path: Path,
    output_dir: Path,
    *,
    dry_run: bool = False,
    describe_images: bool = False,
    local_base_url: str = DEFAULT_LOCAL_BASE_URL,
) -> list[Path]:
    written: list[Path] = []
    for record in iter_activity_records(json_path):
        target = output_path(record, output_dir)
        rendered = render_markdown(
            record,
            source_json=json_path,
            describe_images=describe_images,
            local_base_url=local_base_url,
        )
        if not dry_run:
            if not target.exists() or target.read_text(encoding="utf-8") != rendered:
                atomic_write(target, rendered)
            append_manifest(
                output_dir,
                {
                    "event": "record_rendered",
                    "source_record_id": record.source_record_id,
                    "source_product": record.source_product,
                    "output": str(target),
                    "content_hash": record.content_hash,
                },
            )
        written.append(target)
    return written


def process_input(
    input_path: Path,
    output_dir: Path,
    *,
    work_dir: Path | None = None,
    dry_run: bool = False,
    describe_images: bool = False,
    local_base_url: str = DEFAULT_LOCAL_BASE_URL,
) -> list[Path]:
    input_path = input_path.resolve()
    if input_path.is_file() and input_path.name.lower().endswith(ARCHIVE_SUFFIXES):
        validation = validate_archive(input_path)
        extraction_root = (work_dir or output_dir / "_article_pipeline" / "extracted") / validation.sha256[:16]
        if dry_run:
            return []
        root = extract_archive(validation, extraction_root)
        append_manifest(
            output_dir,
            {
                "event": "archive_extracted",
                "archive": str(input_path),
                "archive_sha256": validation.sha256,
                "member_count": len(validation.members),
            },
        )
    else:
        root = input_path
    json_paths = discover_activity_jsons(root)
    if not json_paths:
        raise ValueError(f"No MyActivity.json files found under {root}")
    outputs: list[Path] = []
    for json_path in json_paths:
        try:
            outputs.extend(
                process_json(
                    json_path,
                    output_dir,
                    dry_run=dry_run,
                    describe_images=describe_images,
                    local_base_url=local_base_url,
                )
            )
        except ValueError as exc:
            if "Could not infer" not in str(exc):
                raise
    return sorted(set(outputs))


def discover_archives(inbox: Path) -> list[Path]:
    if not inbox.exists():
        return []
    return sorted(path for path in inbox.iterdir() if path.is_file() and path.name.lower().endswith(ARCHIVE_SUFFIXES))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="Validate an archive without extracting it.")
    validate.add_argument("archive", type=Path)
    process = subparsers.add_parser("process", help="Process an archive, directory, or MyActivity.json file.")
    process.add_argument("input", type=Path)
    process.add_argument("--output", type=Path, default=Path("./staging"))
    process.add_argument("--work-dir", type=Path)
    process.add_argument("--dry-run", action="store_true")
    process.add_argument("--describe-images", action="store_true")
    process.add_argument("--local-base-url", default=DEFAULT_LOCAL_BASE_URL)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.command == "validate":
        result = validate_archive(args.archive)
        print(json.dumps(dataclasses.asdict(result), default=str, indent=2))
        return 0
    outputs = process_input(
        args.input,
        args.output,
        work_dir=args.work_dir,
        dry_run=args.dry_run,
        describe_images=args.describe_images,
        local_base_url=args.local_base_url,
    )
    print(json.dumps([str(path) for path in outputs], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
