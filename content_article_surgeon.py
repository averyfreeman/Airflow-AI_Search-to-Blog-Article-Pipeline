#!/usr/bin/env python3
"""
content_article_surgeon.py

Purpose:
  1. Convert HTML fragments in Markdown files into proper Markdown.
  2. Detect substantial topic divergences in conversation-derived articles and split
     the divergent tail into a new Markdown article with fresh front matter.
  3. Remove obvious conversation/transcript fragments such as "Your prompt:" and
     "Search's response:" while preserving technical content and code blocks.

Designed for:
  - Python 3.11
  - macOS / Apple Silicon
  - llama.cpp server at http://127.0.0.1:11400/v1
  - OpenRouter chat completions endpoint

Recommended first run:
  python3.11 content_article_surgeon.py --source ./content --dry-run

Then:
  python3.11 content_article_surgeon.py --source ./content
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Any, Iterable, Literal

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as html_to_markdown

from activity_pipeline import discover_single_model, strip_thought_channels
from taxonomy import category_from_text

# =====================================================================
# CONFIGURATION MANAGEMENT
# =====================================================================

SOURCE_DIR = Path("./articles")
PIPELINE_DIRNAME = "_article_pipeline"
PROGRESS_LOG_NAME = "article_surgeon_progress_tree.md"
MANIFEST_NAME = "manifest.jsonl"
BACKUP_DIRNAME = "backups"

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "meta-llama/llama-3.3-70b-instruct:floor"

LOCAL_LLM_BASE_URL = os.environ.get("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11400/v1")
LOCAL_LLM_MODEL = os.environ.get("LOCAL_LLM_MODEL", "auto")

# Conservative by default. The local server is intentionally serial.
LOCAL_SEMAPHORE = asyncio.Semaphore(1)
REMOTE_SEMAPHORE = asyncio.Semaphore(1)
FILE_SEMAPHORE = asyncio.Semaphore(1)

# =====================================================================
# REGEX PATTERNS
# =====================================================================

FENCE_RE = re.compile(r"(^```.*?$.*?^```\s*$|^~~~.*?$.*?^~~~\s*$)", re.M | re.S)
FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9:-]*(?:\s+[^<>]*)?>")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
SETEXT_TITLE_RE = re.compile(r"^(.+?)\n(=+|-+)\s*$", re.M)

CONVERSATION_MARKER_ONLY_RE = re.compile(
    r"^\s*(?:"
    r"your\s+prompt|"
    r"prompt|"
    r"user\s*prompt|"
    r"user|"
    r"assistant|"
    r"assistant\s+response|"
    r"chatgpt|"
    r"search(?:'s)?\s+response|"
    r"model\s+response|"
    r"response"
    r")\s*:\s*$",
    re.I,
)

CONVERSATION_MARKER_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"your\s+prompt|"
    r"user\s+prompt|"
    r"assistant\s+response|"
    r"search(?:'s)?\s+response"
    r")\s*:\s*",
    re.I,
)

LINE_NUMBER_PREFIX_RE = re.compile(r"^\s*\d+\s*[│|:]\s?")
TOKEN_RE = re.compile(r"__PROTECTED_BLOCK_(\d+)__")
AMBIGUOUS_TRANSCRIPT_RE = re.compile(
    r"^\s*(?:you\s+said|chatgpt\s+said|assistant\s+(?:said|replied)|model\s+said|human)\s*:?\s*$",
    re.I | re.M,
)

# =====================================================================
# DATA STRUCTURES
# =====================================================================

Provider = Literal["openrouter", "local", "mlx", "none"]


@dataclasses.dataclass
class PipelineConfig:
    source_dir: Path
    articles: tuple[Path, ...] | None
    dry_run: bool
    recursive: bool
    openrouter_base_url: str
    openrouter_model: str
    openrouter_key: str
    local_base_url: str
    local_model: str
    split_evaluator: Provider
    conversation_auditor: Provider
    min_split_confidence: float
    min_segment_chars: int
    max_heading_map_chars: int
    max_audit_window_lines: int
    max_audit_window_chars: int
    temperature: float
    timeout: float
    force: bool


@dataclasses.dataclass
class FileChange:
    path: Path
    original_sha256: str
    changed: bool = False
    html_converted: bool = False
    conversation_lines_removed: int = 0
    split_files_created: list[Path] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)


# =====================================================================
# FILE AND MANIFEST HELPERS
# =====================================================================


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_slug(text: str, max_len: int = 70) -> str:
    text = re.sub(r"[`*_~>#\[\]()]", "", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return (text[:max_len].strip("_") or "split_article")


def pipeline_dir(source_dir: Path) -> Path:
    return source_dir / PIPELINE_DIRNAME


def manifest_path(source_dir: Path) -> Path:
    return pipeline_dir(source_dir) / MANIFEST_NAME


def backups_dir(source_dir: Path) -> Path:
    return pipeline_dir(source_dir) / BACKUP_DIRNAME


def progress_log_path(source_dir: Path) -> Path:
    return source_dir / PROGRESS_LOG_NAME


def ensure_pipeline_dirs(source_dir: Path) -> None:
    pipeline_dir(source_dir).mkdir(parents=True, exist_ok=True)
    backups_dir(source_dir).mkdir(parents=True, exist_ok=True)


def append_manifest(source_dir: Path, event: dict[str, Any]) -> None:
    ensure_pipeline_dirs(source_dir)
    event = {"timestamp": utc_now(), **event}
    with manifest_path(source_dir).open("a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def load_processed_hashes(source_dir: Path) -> set[str]:
    path = manifest_path(source_dir)
    if not path.exists():
        return set()
    processed: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            item.get("event") == "file_processed"
            and not item.get("dry_run")
            and item.get("original_sha256")
        ):
            processed.add(str(item["original_sha256"]))
    return processed


def backup_file(source_dir: Path, file_path: Path, original_text: str, digest: str) -> Path:
    rel_parent = file_path.parent.relative_to(source_dir)
    target_dir = backups_dir(source_dir) / rel_parent
    target_dir.mkdir(parents=True, exist_ok=True)
    backup_name = f"{file_path.stem}.{digest[:12]}.original{file_path.suffix}"
    backup_path = target_dir / backup_name
    if not backup_path.exists():
        backup_path.write_text(original_text, encoding="utf-8")
    return backup_path


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def should_skip_path(path: Path, source_dir: Path) -> bool:
    parts = set(path.relative_to(source_dir).parts)
    if PIPELINE_DIRNAME in parts:
        return True
    if path.name == PROGRESS_LOG_NAME:
        return True
    if path.name.endswith(".tmp"):
        return True
    if path.name.endswith(".eval.json"):
        return True
    if path.name.startswith("."):
        return True
    return False


def iter_markdown_files(source_dir: Path, recursive: bool) -> list[Path]:
    globber = source_dir.rglob if recursive else source_dir.glob
    files = [p for p in globber("*.md") if p.is_file() and not should_skip_path(p, source_dir)]
    return sorted(files)


# =====================================================================
# PROTECTED BLOCK HELPERS
# =====================================================================


def protect_front_matter_and_code(text: str) -> tuple[str, dict[str, str]]:
    """Protect YAML front matter and fenced code blocks from Markdown surgery."""
    protected: dict[str, str] = {}
    counter = 0

    def stash(block: str) -> str:
        nonlocal counter
        token = f"__PROTECTED_BLOCK_{counter}__"
        protected[token] = block
        counter += 1
        return token

    fm = FRONT_MATTER_RE.match(text)
    prefix = ""
    rest = text
    if fm:
        full = fm.group(0)
        prefix = stash(full) + "\n"
        rest = text[len(full):]

    def fence_replacer(match: re.Match[str]) -> str:
        return stash(match.group(0))

    rest = FENCE_RE.sub(fence_replacer, rest)
    return prefix + rest, protected


def restore_protected(text: str, protected: dict[str, str]) -> str:
    for token, block in protected.items():
        text = text.replace(token, block.rstrip("\n"))
    return text


# =====================================================================
# FRONT MATTER HELPERS
# =====================================================================


def split_front_matter(text: str) -> tuple[str | None, str]:
    match = FRONT_MATTER_RE.match(text)
    if not match:
        return None, text
    return match.group(0).rstrip("\n"), text[len(match.group(0)):].lstrip("\n")


def parse_front_matter_title(text: str) -> str | None:
    fm, body = split_front_matter(text)
    if fm:
        for line in fm.splitlines():
            if re.match(r"^title\s*:", line, flags=re.I):
                value = line.split(":", 1)[1].strip().strip('"\'')
                if value:
                    return value
    heading = first_heading(body)
    return heading


def format_front_matter(title: str, tags: list[str], source_file: str, split_from_line: int | None = None) -> str:
    safe_title = title.replace('"', "'").strip() or "Untitled Article"
    tags = [t.strip().replace('"', "'") for t in tags if t and t.strip()]
    if not tags:
        tags = ["Article"]
    category_label = tags[0]
    lines = [
        "---",
        f'Title: "{safe_title}"',
        f'Date: "{dt.datetime.now().strftime("%Y-%m-%d_%H_%M")}"',
        f'Category: "{category_label}"',
        "Tags:",
    ]
    lines.extend([f"  - {tag}" for tag in tags[:8]])
    lines.append(f'Source: "{source_file}"')
    if split_from_line is not None:
        lines.append(f"Split_From_Line: {split_from_line}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


# =====================================================================
# HTML TO MARKDOWN CONVERSION
# =====================================================================


def looks_like_html_fragment(text: str) -> bool:
    if not HTML_TAG_RE.search(text):
        return False
    # Avoid false positives such as generic type parameters in prose.
    tag_names = re.findall(r"</?([A-Za-z][A-Za-z0-9:-]*)\b", text)
    if not tag_names:
        return False
    common_html = {
        "a", "abbr", "article", "aside", "b", "blockquote", "br", "button", "caption",
        "code", "col", "colgroup", "dd", "del", "details", "div", "dl", "dt", "em",
        "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header",
        "hr", "i", "img", "input", "kbd", "label", "li", "main", "nav", "ol", "p", "pre",
        "s", "section", "select", "small", "span", "strong", "sub", "summary", "sup",
        "table", "tbody", "td", "textarea", "tfoot", "th", "thead", "tr", "u", "ul",
    }
    return any(t.lower() in common_html for t in tag_names)


def convert_html_fragments(segment: str) -> tuple[str, bool]:
    """
    Convert HTML fragments while minimizing changes to ordinary Markdown.

    Strategy:
      - Strip HTML comments.
      - Work paragraph-by-paragraph outside protected code/front matter.
      - Convert only paragraphs that contain plausible HTML tags.
    """
    changed = False
    segment2 = HTML_COMMENT_RE.sub("", segment)
    if segment2 != segment:
        changed = True
        segment = segment2

    blocks = re.split(r"(\n\s*\n)", segment)
    out: list[str] = []

    for block in blocks:
        if not block or re.fullmatch(r"\n\s*\n", block):
            out.append(block)
            continue
        if not looks_like_html_fragment(block):
            out.append(block)
            continue

        converted = html_to_markdown(
            block,
            heading_style="ATX",
            bullets="-",
            strip=["script", "style"],
        )
        converted = normalize_markdown_spacing(converted)
        if converted.strip() and converted != block:
            out.append(converted)
            changed = True
        else:
            out.append(block)

    return "".join(out), changed


def normalize_markdown_spacing(text: str) -> str:
    text = text.replace("\r\n", "\n")
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip("\n") + "\n"


# =====================================================================
# CONVERSATION FRAGMENT CLEANUP
# =====================================================================


def deterministic_conversation_cleanup(text: str) -> tuple[str, int]:
    protected_text, protected = protect_front_matter_and_code(text)
    removed = 0
    output_lines: list[str] = []

    for line in protected_text.splitlines():
        raw = line
        if TOKEN_RE.fullmatch(raw.strip()):
            output_lines.append(raw)
            continue

        stripped_numbered = LINE_NUMBER_PREFIX_RE.sub("", raw)
        if CONVERSATION_MARKER_ONLY_RE.match(stripped_numbered):
            removed += 1
            continue

        # Strip only very explicit transcript prefixes. Do not strip generic "User:" because it
        # may be meaningful in documentation or examples.
        stripped_prefix = CONVERSATION_MARKER_PREFIX_RE.sub("", stripped_numbered)
        if stripped_prefix != stripped_numbered:
            removed += 1
            if stripped_prefix.strip():
                output_lines.append(stripped_prefix)
            continue

        output_lines.append(raw)

    cleaned = "\n".join(output_lines)
    cleaned = restore_protected(cleaned, protected)
    cleaned = normalize_markdown_spacing(cleaned)
    return cleaned, removed


def needs_conversation_audit(text: str) -> bool:
    """Reserve model auditing for transcript-like labels the deterministic pass cannot classify."""

    _, body = split_front_matter(text)
    return len(body.strip()) >= 160 and bool(AMBIGUOUS_TRANSCRIPT_RE.search(body))


def line_numbered_window(lines: list[str], start_index: int, end_index: int) -> str:
    out = []
    for i in range(start_index, end_index):
        out.append(f"{i + 1}: {lines[i]}")
    return "\n".join(out)


def remove_line_numbers(text: str, line_numbers: set[int]) -> str:
    lines = text.splitlines()
    kept = [line for idx, line in enumerate(lines, start=1) if idx not in line_numbers]
    return normalize_markdown_spacing("\n".join(kept))


# =====================================================================
# HEADING MAP AND SPLIT HELPERS
# =====================================================================


def first_heading(text: str) -> str | None:
    for line in text.splitlines():
        m = HEADING_RE.match(line)
        if m:
            return clean_heading_text(m.group(2))
    m2 = SETEXT_TITLE_RE.search(text[:2000])
    if m2:
        return clean_heading_text(m2.group(1))
    return None


def clean_heading_text(text: str) -> str:
    text = re.sub(r"#+\s*$", "", text).strip()
    text = re.sub(r"[`*_\[\]()]+", "", text).strip()
    return text


def get_heading_positions(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    headings: list[dict[str, Any]] = []
    for idx, line in enumerate(lines, start=1):
        m = HEADING_RE.match(line)
        if m:
            headings.append({
                "line": idx,
                "level": len(m.group(1)),
                "title": clean_heading_text(m.group(2)),
            })
    return headings


def needs_topic_split(text: str, min_segment_chars: int) -> bool:
    """Only ask the model when a split is both plausible and mechanically applicable."""

    _, body = split_front_matter(text)
    return len(body.strip()) >= min_segment_chars * 2 and len(get_heading_positions(body)) >= 2


def build_heading_map(text: str, max_chars: int) -> str:
    lines = text.splitlines()
    headings = get_heading_positions(text)
    if not headings:
        sample = "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines[:120]))
        return sample[:max_chars]

    chunks: list[str] = []
    for h in headings:
        line_num = int(h["line"])
        start = max(1, line_num - 2)
        end = min(len(lines), line_num + 8)
        excerpt = line_numbered_window(lines, start - 1, end)
        chunks.append(f"\n--- HEADING CANDIDATE line {line_num} ---\n{excerpt}")

    combined = "\n".join(chunks)
    if len(combined) <= max_chars:
        return combined

    # Preserve beginning and ending heading context if very long.
    half = max_chars // 2
    return combined[:half] + "\n\n[... middle heading map omitted for length ...]\n\n" + combined[-half:]


def nearest_heading_line(requested_line: int, headings: list[dict[str, Any]], max_distance: int = 8) -> int | None:
    if not headings:
        return None
    candidates = sorted((abs(int(h["line"]) - requested_line), int(h["line"])) for h in headings)
    distance, line = candidates[0]
    if distance <= max_distance:
        return line
    return None


def split_text_at_line(text: str, line_num: int) -> tuple[str, str]:
    lines = text.splitlines()
    before = "\n".join(lines[: line_num - 1]).rstrip() + "\n"
    after = "\n".join(lines[line_num - 1 :]).lstrip("\n") + "\n"
    return before, after


def strip_front_matter_for_segment(text: str) -> str:
    _, body = split_front_matter(text)
    return body.lstrip("\n")


# =====================================================================
# JSON AND API HELPERS
# =====================================================================


def extract_json_object(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        parsed = json.loads(raw[start : end + 1])
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"Could not parse JSON object from model output: {raw[:300]!r}")


def completion_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


async def chat_completion(
    client: httpx.AsyncClient,
    provider: Provider,
    config: PipelineConfig,
    messages: list[dict[str, str]],
    json_mode: bool = False,
    temperature: float | None = None,
) -> str:
    if provider == "none":
        raise ValueError("Provider 'none' cannot make chat completions.")

    if provider == "openrouter":
        if not config.openrouter_key:
            raise RuntimeError("OPENROUTER_API_KEY is required for OpenRouter calls.")
        url = completion_url(config.openrouter_base_url)
        headers = {
            "Authorization": f"Bearer {config.openrouter_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost/content-article-surgeon",
            "X-Title": "content-article-surgeon",
        }
        model = config.openrouter_model
        semaphore = REMOTE_SEMAPHORE
    else:
        url = completion_url(config.local_base_url)
        headers = {"Content-Type": "application/json"}
        model = config.local_model
        semaphore = LOCAL_SEMAPHORE

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": config.temperature if temperature is None else temperature,
        "max_tokens": 1600,
    }
    if provider in {"local", "mlx"}:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    async with semaphore:
        response = await client.post(url, headers=headers, json=payload, timeout=config.timeout)
        response.raise_for_status()
        data = response.json()
        return strip_thought_channels(data["choices"][0]["message"]["content"])


# =====================================================================
# LLM TASKS
# =====================================================================


async def llm_conversation_audit(
    client: httpx.AsyncClient,
    config: PipelineConfig,
    text: str,
) -> tuple[set[int], list[str]]:
    if config.conversation_auditor == "none":
        return set(), []

    protected_text, protected = protect_front_matter_and_code(text)
    # Remove protected blocks from what the model sees. We never ask it to delete code/front matter lines.
    visible = protected_text
    for token in protected:
        visible = visible.replace(token, "[PROTECTED CODE OR FRONT MATTER BLOCK]")

    lines = visible.splitlines()
    remove: set[int] = set()
    notes: list[str] = []

    start = 0
    while start < len(lines):
        end = min(len(lines), start + config.max_audit_window_lines)
        window = line_numbered_window(lines, start, end)
        if len(window) > config.max_audit_window_chars:
            # Shrink the line window if lines are very long.
            while end > start + 20 and len(window) > config.max_audit_window_chars:
                end = start + max(20, (end - start) // 2)
                window = line_numbered_window(lines, start, end)

        system_prompt = (
            "You are a conservative Markdown transcript-cleanup auditor. Return JSON only. "
            "Identify line numbers that are pure conversation scaffolding or transcript labels, "
            "such as 'Your prompt:', 'Search response:', 'Assistant:', or duplicate chat export markers. "
            "Do not remove technical content, headings, commands, code descriptions, article prose, "
            "or lines that are merely written in first person. If uncertain, keep the line."
        )
        user_prompt = (
            "Return this JSON schema exactly:\n"
            '{"remove_line_numbers": [1, 2], "notes": ["brief reason"]}\n\n'
            "Line-numbered Markdown excerpt:\n"
            f"{window}"
        )
        try:
            raw = await chat_completion(
                client,
                config.conversation_auditor,
                config,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                json_mode=True,
                temperature=0.0,
            )
            parsed = extract_json_object(raw)
            for n in parsed.get("remove_line_numbers", []):
                try:
                    n_int = int(n)
                except Exception:
                    continue
                if start + 1 <= n_int <= end:
                    remove.add(n_int)
            for note in parsed.get("notes", []):
                if isinstance(note, str) and note.strip():
                    notes.append(note.strip())
        except Exception as exc:
            notes.append(f"conversation audit skipped for lines {start + 1}-{end}: {exc}")

        start = end

    return remove, notes


async def llm_detect_topic_splits(
    client: httpx.AsyncClient,
    config: PipelineConfig,
    file_path: Path,
    text: str,
) -> dict[str, Any]:
    if config.split_evaluator == "none":
        return {"split_recommended": False, "split_points": []}

    title = parse_front_matter_title(text) or file_path.stem
    heading_map = build_heading_map(text, config.max_heading_map_chars)

    system_prompt = (
        "You are a careful editorial classifier for conversation-derived Markdown articles. "
        "Return JSON only. Your job is to detect whether one Markdown file contains a sharp, "
        "substantial topic divergence where the later section should become a separate article. "
        "Be conservative: do not split merely because a subsection narrows the same topic. "
        "Recommend a split only when the later material has a different reader intent, different "
        "technical subject, or would need a separate title and one broad category. Prefer split points at headings. "
        "Do not invent narrow tags or folders; the downstream taxonomy classifier supplies the category."
    )
    user_prompt = (
        "Analyze this article heading map and excerpts. Return JSON using this schema:\n"
        "{\n"
        '  "split_recommended": true,\n'
        '  "split_points": [\n'
        "    {\n"
        '      "line": 35,\n'
        '      "confidence": 0.91,\n'
        '      "new_title": "Running GGUF Models with 64K+ Context",\n'
        '      "new_category": "AI",\n'
        '      "reasoning": "The article changes from assistant-platform comparison to local model inference."\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "- Only recommend split_points at or near a Markdown heading line.\n"
        "- Use confidence 0.0 to 1.0.\n"
        "- A split point means: keep everything before that line in the original file, "
        "and move that line plus the rest into a new article.\n"
        "- If no split is clearly needed, return {\"split_recommended\": false, \"split_points\": []}.\n\n"
        f"Current/inferred title: {title}\n"
        f"File: {file_path.name}\n\n"
        f"Heading map and excerpts:\n{heading_map}"
    )

    raw = await chat_completion(
        client,
        config.split_evaluator,
        config,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        json_mode=True,
        temperature=0.0,
    )
    return extract_json_object(raw)


# =====================================================================
# SPLIT APPLICATION
# =====================================================================


def normalize_split_points(
    text: str,
    decision: dict[str, Any],
    config: PipelineConfig,
) -> list[dict[str, Any]]:
    if not decision.get("split_recommended"):
        return []
    headings = get_heading_positions(text)
    if not headings:
        return []

    text_lines = text.splitlines()
    output: list[dict[str, Any]] = []
    seen: set[int] = set()

    for point in decision.get("split_points", []):
        if not isinstance(point, dict):
            continue
        try:
            requested_line = int(point.get("line"))
            confidence = float(point.get("confidence", 0.0))
        except Exception:
            continue
        if confidence < config.min_split_confidence:
            continue
        line = nearest_heading_line(requested_line, headings)
        if line is None or line in seen:
            continue
        before = "\n".join(text_lines[: line - 1])
        after = "\n".join(text_lines[line - 1 :])
        if len(before.strip()) < config.min_segment_chars or len(after.strip()) < config.min_segment_chars:
            continue
        title = str(point.get("new_title") or "").strip()
        if not title:
            heading = next((h["title"] for h in headings if int(h["line"]) == line), "Split Article")
            title = str(heading)
        tags = point.get("new_tags") if isinstance(point.get("new_tags"), list) else []
        output.append({
            "line": line,
            "confidence": confidence,
            "new_title": title,
            "new_tags": [str(t) for t in tags][:1],
            "new_category": str(point.get("new_category", "")).strip(),
            "reasoning": str(point.get("reasoning", "")).strip(),
        })
        seen.add(line)

    return sorted(output, key=lambda x: int(x["line"]))


def apply_splits(
    source_dir: Path,
    file_path: Path,
    text: str,
    split_points: list[dict[str, Any]],
    dry_run: bool,
) -> tuple[str, list[Path]]:
    if not split_points:
        return text, []

    # For safety, currently apply the first substantial divergence only. This matches the
    # common "article A abruptly becomes article B" failure mode and avoids over-fragmenting.
    point = split_points[0]
    line = int(point["line"])
    before, after = split_text_at_line(text, line)

    title = str(point["new_title"])
    new_category = category_from_text(after, folder=file_path.parent.name)
    tags = [new_category.label]
    new_fm = format_front_matter(title, tags, file_path.name, split_from_line=line)
    new_body = strip_front_matter_for_segment(after)
    new_text = normalize_markdown_spacing(new_fm + new_body)

    rel_parent = file_path.parent
    slug = safe_slug(title)
    new_name = f"{file_path.stem}__split_{line}_{slug}{file_path.suffix}"
    new_path = rel_parent / new_name
    counter = 2
    while new_path.exists():
        new_path = rel_parent / f"{file_path.stem}__split_{line}_{slug}_{counter}{file_path.suffix}"
        counter += 1

    if not dry_run:
        atomic_write_text(new_path, new_text)

    return normalize_markdown_spacing(before), [new_path]


# =====================================================================
# PROGRESS TREE
# =====================================================================


def generate_progress_tree(source_dir: Path, changes: list[FileChange] | None = None) -> None:
    lines = ["# Article Surgeon Progress Tree", "", "```text"]
    files = iter_markdown_files(source_dir, recursive=True)
    for p in files:
        rel = p.relative_to(source_dir)
        lines.append(f"📄 {rel}")
    if changes:
        lines.append("")
        lines.append("Recent changes:")
        for ch in changes[-50:]:
            rel = ch.path.relative_to(source_dir)
            flags = []
            if ch.html_converted:
                flags.append("html→md")
            if ch.conversation_lines_removed:
                flags.append(f"removed {ch.conversation_lines_removed} transcript lines")
            if ch.split_files_created:
                flags.append(f"created {len(ch.split_files_created)} split file(s)")
            if not flags:
                flags.append("no-op")
            lines.append(f"  - {rel}: {', '.join(flags)}")
    lines.append("```")
    progress_log_path(source_dir).write_text("\n".join(lines) + "\n", encoding="utf-8")


# =====================================================================
# CORE FILE PROCESSOR
# =====================================================================


async def process_file(client: httpx.AsyncClient, config: PipelineConfig, file_path: Path) -> FileChange:
    async with FILE_SEMAPHORE:
        original = file_path.read_text(encoding="utf-8")
        original_hash = sha256_text(original)
        change = FileChange(path=file_path, original_sha256=original_hash)
        working = original

        protected_text, protected = protect_front_matter_and_code(working)
        converted, html_changed = convert_html_fragments(protected_text)
        working = restore_protected(converted, protected)
        working = normalize_markdown_spacing(working)
        change.html_converted = html_changed
        change.changed = change.changed or html_changed

        working, deterministic_removed = deterministic_conversation_cleanup(working)
        change.conversation_lines_removed += deterministic_removed
        change.changed = change.changed or deterministic_removed > 0

        llm_removed: set[int] = set()
        llm_notes: list[str] = []
        if config.conversation_auditor != "none" and needs_conversation_audit(working):
            front_matter, audit_body = split_front_matter(working)
            llm_removed, llm_notes = await llm_conversation_audit(client, config, audit_body)
            if llm_removed:
                audit_body = remove_line_numbers(audit_body, llm_removed)
                working = f"{front_matter}\n\n{audit_body}" if front_matter else audit_body
                change.conversation_lines_removed += len(llm_removed)
                change.changed = True
            change.notes.extend(llm_notes)

        split_points: list[dict[str, Any]] = []
        if config.split_evaluator != "none" and needs_topic_split(working, config.min_segment_chars):
            try:
                decision = await llm_detect_topic_splits(client, config, file_path, working)
                split_points = normalize_split_points(working, decision, config)
            except Exception as exc:
                change.notes.append(f"split detection skipped: {exc}")

        if split_points:
            working, new_files = apply_splits(config.source_dir, file_path, working, split_points, config.dry_run)
            change.split_files_created = new_files
            change.changed = True
            for point in split_points[:1]:
                if point.get("reasoning"):
                    change.notes.append(f"split at line {point['line']}: {point['reasoning']}")

        if change.changed:
            if config.dry_run:
                rel_parent = file_path.parent.relative_to(config.source_dir)
                backup_name = f"{file_path.stem}.{original_hash[:12]}.original{file_path.suffix}"
                would_backup = backups_dir(config.source_dir) / rel_parent / backup_name
                change.notes.append(f"dry-run: backup would be {would_backup.relative_to(config.source_dir)}")
            else:
                backup_path = backup_file(config.source_dir, file_path, original, original_hash)
                change.notes.append(f"backup: {backup_path.relative_to(config.source_dir)}")
                atomic_write_text(file_path, working)

        append_manifest(config.source_dir, {
            "event": "file_processed",
            "dry_run": config.dry_run,
            "path": str(file_path.relative_to(config.source_dir)),
            "original_sha256": original_hash,
            "changed": change.changed,
            "html_converted": change.html_converted,
            "conversation_lines_removed": change.conversation_lines_removed,
            "split_files_created": [str(p.relative_to(config.source_dir)) for p in change.split_files_created],
            "notes": change.notes,
        })
        return change


# =====================================================================
# ENGINE CONTROL FLOW
# =====================================================================


def parse_args(argv: list[str]) -> PipelineConfig:
    parser = argparse.ArgumentParser(
        description="Clean conversation-derived Markdown, convert HTML fragments, and split sharp topic divergences.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--source", type=Path, default=SOURCE_DIR, help="Directory containing Markdown files.")
    parser.add_argument(
        "--article",
        type=Path,
        action="append",
        help="Process only this Markdown file. May be repeated; paths must be within --source.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Analyze and log changes without mutating Markdown files.")
    parser.add_argument("--no-recursive", action="store_true", help="Only process Markdown files directly inside --source.")
    parser.add_argument("--force", action="store_true", help="Reprocess files even if their original hash appears in the manifest.")

    parser.add_argument(
        "--local-base-url",
        "--mlx-base-url",
        dest="local_base_url",
        default=LOCAL_LLM_BASE_URL,
        help="Base URL ending in /v1 for the local OpenAI-compatible server.",
    )
    parser.add_argument(
        "--local-model",
        "--mlx-model",
        dest="local_model",
        default=LOCAL_LLM_MODEL,
        help="Local model ID, or 'auto' to require exactly one served model.",
    )
    parser.add_argument("--openrouter-base-url", default=OPENROUTER_BASE_URL, help="OpenRouter base URL ending in /v1.")
    parser.add_argument("--openrouter-model", default=OPENROUTER_MODEL, help="OpenRouter model for split evaluation.")
    parser.add_argument("--openrouter-key", default=os.environ.get("OPENROUTER_API_KEY", ""), help="OpenRouter API key. Prefer OPENROUTER_API_KEY env var.")

    parser.add_argument(
        "--split-evaluator",
        choices=["openrouter", "local", "mlx", "none"],
        default="local",
        help="Provider used to detect substantial topic divergences; 'mlx' is a compatibility alias for local.",
    )
    parser.add_argument(
        "--conversation-auditor",
        choices=["local", "mlx", "openrouter", "none"],
        default="local",
        help="Provider used to identify non-obvious conversation fragments.",
    )
    parser.add_argument("--min-split-confidence", type=float, default=0.82, help="Minimum model confidence before applying a split.")
    parser.add_argument("--min-segment-chars", type=int, default=1800, help="Minimum chars required before and after a split.")
    parser.add_argument("--max-heading-map-chars", type=int, default=24000, help="Max chars sent to split evaluator.")
    parser.add_argument("--max-audit-window-lines", type=int, default=280, help="Lines per conversation-fragment audit request.")
    parser.add_argument("--max-audit-window-chars", type=int, default=24000, help="Chars per conversation-fragment audit request.")
    parser.add_argument("--temperature", type=float, default=0.0, help="Temperature for LLM classification calls.")
    parser.add_argument("--timeout", type=float, default=180.0, help="HTTP timeout seconds.")
    parser.add_argument("--local-concurrency", type=int, default=2, help="Concurrency limit for local llama.cpp calls.")
    parser.add_argument("--remote-concurrency", type=int, default=1, help="Concurrency limit for OpenRouter calls.")
    parser.add_argument("--file-concurrency", type=int, default=1, help="Number of Markdown files processed concurrently.")

    ns = parser.parse_args(argv)

    global LOCAL_SEMAPHORE, REMOTE_SEMAPHORE, FILE_SEMAPHORE
    LOCAL_SEMAPHORE = asyncio.Semaphore(max(1, ns.local_concurrency))
    REMOTE_SEMAPHORE = asyncio.Semaphore(max(1, ns.remote_concurrency))
    FILE_SEMAPHORE = asyncio.Semaphore(max(1, ns.file_concurrency))

    return PipelineConfig(
        source_dir=ns.source,
        articles=tuple(ns.article) if ns.article else None,
        dry_run=ns.dry_run,
        recursive=not ns.no_recursive,
        openrouter_base_url=ns.openrouter_base_url,
        openrouter_model=ns.openrouter_model,
        openrouter_key=ns.openrouter_key,
        local_base_url=ns.local_base_url,
        local_model=ns.local_model,
        split_evaluator=ns.split_evaluator,
        conversation_auditor=ns.conversation_auditor,
        min_split_confidence=ns.min_split_confidence,
        min_segment_chars=ns.min_segment_chars,
        max_heading_map_chars=ns.max_heading_map_chars,
        max_audit_window_lines=ns.max_audit_window_lines,
        max_audit_window_chars=ns.max_audit_window_chars,
        temperature=ns.temperature,
        timeout=ns.timeout,
        force=ns.force,
    )


async def main_async(argv: list[str]) -> int:
    config = parse_args(argv)
    source_dir = config.source_dir

    local_providers = {"local", "mlx"}
    uses_local_model = (
        config.split_evaluator in local_providers
        or config.conversation_auditor in local_providers
    )
    if uses_local_model and config.local_model == "auto":
        config.local_model = discover_single_model(config.local_base_url)
        print(f"Local model: {Path(config.local_model).name}")

    if not source_dir.exists():
        print(f"Source directory not found: {source_dir}", file=sys.stderr)
        return 2
    if not source_dir.is_dir():
        print(f"Source path is not a directory: {source_dir}", file=sys.stderr)
        return 2

    ensure_pipeline_dirs(source_dir)
    processed_hashes = load_processed_hashes(source_dir) if not config.force else set()
    if config.articles:
        source_resolved = source_dir.resolve()
        all_files = []
        for requested in config.articles:
            candidate = requested.resolve()
            try:
                candidate.relative_to(source_resolved)
            except ValueError as exc:
                raise ValueError(f"--article must be inside --source: {candidate}") from exc
            if candidate.is_file() and candidate.suffix.lower() in {".md", ".mdx"}:
                all_files.append(candidate)
        all_files = sorted(set(all_files))
    else:
        all_files = iter_markdown_files(source_dir, recursive=config.recursive)
    files = []
    for p in all_files:
        try:
            digest = sha256_text(p.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            print(f"[SKIP] Not valid UTF-8: {p}")
            continue
        if digest in processed_hashes:
            print(f"[SKIP] Already processed unchanged file: {p.relative_to(source_dir)}")
            continue
        files.append(p)

    print(f"Article Surgeon starting: {len(files)} Markdown file(s) queued. Dry run: {config.dry_run}")
    if config.split_evaluator == "openrouter" and not config.openrouter_key:
        print("[WARN] --split-evaluator openrouter selected, but OPENROUTER_API_KEY is not set.", file=sys.stderr)
    if config.conversation_auditor == "openrouter" and not config.openrouter_key:
        print("[WARN] --conversation-auditor openrouter selected, but OPENROUTER_API_KEY is not set.", file=sys.stderr)

    changes: list[FileChange] = []
    async with httpx.AsyncClient() as client:
        tasks = [process_file(client, config, p) for p in files]
        for coro in asyncio.as_completed(tasks):
            try:
                change = await coro
            except Exception as exc:
                print(f"[ERROR] {exc}", file=sys.stderr)
                continue
            changes.append(change)
            rel = change.path.relative_to(source_dir)
            print(
                f"[DONE] {rel} | changed={change.changed} | html={change.html_converted} | "
                f"removed={change.conversation_lines_removed} | splits={len(change.split_files_created)}"
            )

    generate_progress_tree(source_dir, changes)
    changed_count = sum(1 for c in changes if c.changed)
    split_count = sum(len(c.split_files_created) for c in changes)
    print(f"Finished. Files changed: {changed_count}. Split files created: {split_count}.")
    print(f"Manifest: {manifest_path(source_dir)}")
    print(f"Progress: {progress_log_path(source_dir)}")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(main_async(sys.argv[1:])))


if __name__ == "__main__":
    main()
