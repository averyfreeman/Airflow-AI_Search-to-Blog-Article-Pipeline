#!/usr/bin/env python3
"""Controlled article taxonomy and deterministic category helpers.

The publication tree intentionally has one broad category per article.  This
module is shared by migration, validation, and the Takeout pipeline so a model
cannot create a new folder or tag by accident.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


MAX_CATEGORIES = 30
TARGET_CATEGORY_COUNT = 23


@dataclass(frozen=True)
class Category:
    slug: str
    label: str
    description: str


CATEGORIES: tuple[Category, ...] = (
    Category("ai", "AI", "Artificial intelligence, models, agents, and AI services."),
    Category("computing", "Computing", "General computing concepts and cross-platform systems."),
    Category("software-development", "Software Development", "Programming, tools, source code, and development practices."),
    Category("devops-cloud", "DevOps and Cloud", "Cloud services, deployment, containers, and infrastructure operations."),
    Category("linux-unix", "Linux and Unix", "Linux, Unix, shells, package management, and system administration."),
    Category("windows", "Windows", "Windows desktop, server, PowerShell, registry, and administration."),
    Category("networking", "Networking", "Networks, DNS, routing, monitoring, and network services."),
    Category("security-identity", "Security and Identity", "Authentication, authorization, encryption, privacy, and security."),
    Category("databases-data", "Databases and Data", "Databases, data formats, analytics, and information management."),
    Category("web-development", "Web Development", "Web applications, APIs, CMSs, and browser-facing systems."),
    Category("hardware-devices", "Hardware and Devices", "Computers, components, displays, peripherals, and electronics."),
    Category("mobile", "Mobile", "Phones, tablets, mobile operating systems, and mobile applications."),
    Category("audio-music", "Audio and Music", "Music production, DJ equipment, recording, and audio technology."),
    Category("automotive", "Automotive", "Cars, vehicle technology, charging, and automotive services."),
    Category("home", "Home", "Appliances, home automation, and household systems."),
    Category("finance", "Finance", "Personal finance, accounting, investing, and financial services."),
    Category("housing", "Housing", "Housing, real estate, construction, planning, and homelessness services."),
    Category("health", "Health", "Medical, wellness, nutrition, medications, and health risks."),
    Category("food", "Food", "Cooking, food safety, ingredients, and food-related questions."),
    Category("language-education", "Language and Education", "Linguistics, language history, learning, and education systems."),
    Category("society-politics", "Society and Politics", "Government, elections, public policy, history, and society."),
    Category("shopping-consumer", "Shopping and Consumer", "Products, retailers, consumer guidance, and buying decisions."),
    Category("sports-entertainment", "Sports and Entertainment", "Sports, games, television, film, and general entertainment."),
)

CATEGORY_BY_SLUG = {category.slug: category for category in CATEGORIES}
CATEGORY_BY_LABEL = {category.label.casefold(): category for category in CATEGORIES}

PROVENANCE_TAGS = {"search", "ai_mode", "ai mode", "gemini_apps", "gemini apps"}

# Rules are ordered from the most safety-sensitive/specific domains to the
# broad technical fallbacks.  They are deliberately conservative: a caller
# should send low-confidence assignments to editorial review.
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("health", ("health", "medical", "medicine", "medication", "drug", "disease", "vaping", "nicotine", "lung", "symptom", "staph", "cephalexin", "knee", "food tracking", "potassium")),
    ("finance", ("finance", "financial", "accounting", "beancount", "hledger", "ledger", "bank", "loan", "credit", "invest", "market", "trading", "stock", "social security", "ssa", "tax")),
    ("housing", ("housing", "home price", "real estate", "property", "mortgage", "rent", "homeless", "lihi", "construction", "building", "zoning", "urban planning", "r-value", "acre")),
    ("food", ("cooking", "recipe", "food", "cat nutrition", "tuna", "mackerel", "cuisinart", "ice-70")),
    ("language-education", ("linguistic", "language", "etymology", "grammar", "gender", "cantonese", "education", "school", "finnish education")),
    ("society-politics", ("politic", "election", "president", "presidential", "government", "congress", "nrsc", "gaddafi", "terrorism", "public policy")),
    ("automotive", ("automotive", "car", "vehicle", "chevy bolt", "dash cam", "car wash", "charging station", "miles from my location")),
    ("audio-music", ("music", "audio", "microphone", "mic", "dj", "serato", "mixxx", "turntable", "rane", "gemini mdj", "recording")),
    ("mobile", ("android", "iphone", "ipad", "pixel", "fitbit", "wallet", "mobile phone", "mobile app", "authy")),
    ("home", ("home automation", "wyze switch", "appliance", "ceiling tile", "household")),
    ("shopping-consumer", ("shopping", "amazon", "retail", "pharmacy", "product", "consumer", "cuisinart", "best pharmacy")),
    ("sports-entertainment", ("sport", "world cup", "tiebreaker", "gaming", "game", "television", "tv model", "video", "youtube", "birmingham england")),
    ("ai", (" ai ", "artificial intelligence", "llm", "language model", "gemini", "openai", "claude", "llama", "mistral", "prompt", "model", "agent", "mcp", "omnirouter", "openrouter", "transcription", "summarization")),
    ("security-identity", ("security", "secure", "authentication", "identity", "active directory", "kerberos", "ldap", "samba ad", "adfs", "oauth", "openid", "fingerprint login", "encryption", "aes-256", "pci", "ssh key", "private key", "captcha")),
    ("databases-data", ("database", "postgres", "postgresql", "sql", "pgvector", "vector database", "data", "csv", "json", "grafana", "loki", "analytics")),
    ("web-development", ("web", "website", "browser", "frontend", "backend", "crud", "cms", "html", "css", "nextjs", "next.js", "playwright", "api")),
    ("networking", ("network", "dns", "dhcp", "subnet", "router", "switch", "zerotier", "unifi", "snmp", "syslog", "evpn", "vxlan", "coredns", "bridge", "wireless")),
    ("devops-cloud", ("cloud", "oci", "oracle cloud", "cloudflare", "docker", "podman", "container", "kubernetes", "microk8s", "terraform", "deployment", "hosting", "iac", "systemd service", "github container")),
    ("windows", ("windows", "powershell", "win32", "registry", "admx", "winget", "mft_cleanup", "windows server")),
    ("linux-unix", ("linux", "ubuntu", "debian", "fedora", "opensuse", "alpine", "unix", "bash", "zsh", "shell", "nushell", "apt", "dnf", "snap", "zfs", "lxc", "lxd", "qemu", "system administration")),
    ("software-development", ("software", "programming", "python", "rust", "elm", "purescript", "javascript", "typescript", "git", "github", "library", "script", "code", "scripting", "airflow", "dag")),
    ("hardware-devices", ("hardware", "computer", "pc", "laptop", "macbook", "mac", "monitor", "display", "dimm", "ram", "sata", "pcie", "gpu", "arm64", "electronics", "device")),
)


def category(slug_or_label: str) -> Category:
    key = slug_or_label.strip().casefold()
    if key in CATEGORY_BY_SLUG:
        return CATEGORY_BY_SLUG[key]
    if key in CATEGORY_BY_LABEL:
        return CATEGORY_BY_LABEL[key]
    raise ValueError(f"Unknown category: {slug_or_label!r}")


def normalize_tag(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().strip('"\''))


def is_provenance_tag(value: str) -> bool:
    return normalize_tag(value).casefold() in PROVENANCE_TAGS


def parse_tags(frontmatter: str) -> list[str]:
    match = re.search(r"^Tags:\s*\n((?:^[ \t]+- .*\n?)+)", frontmatter, re.M)
    if not match:
        return []
    return [normalize_tag(line.split("-", 1)[1]) for line in match.group(1).splitlines() if "-" in line]


def render_tags(label: str) -> str:
    return f"Tags:\n  - {label}\n"


def replace_tags(frontmatter: str, label: str) -> str:
    # The matched block usually consumes the newline after the final list
    # item. Keep a separator before Source/Category fields.
    replacement = render_tags(label)
    pattern = r"^Tags:\s*\n(?:^[ \t]+- .*\n?)+"
    if re.search(pattern, frontmatter, re.M):
        return re.sub(pattern, replacement, frontmatter, count=1, flags=re.M)
    return frontmatter.rstrip() + "\n" + replacement


def _search_text(title: str, tags: Iterable[str], folder: str, body: str) -> str:
    # Keep the body bounded: headings and the opening provide useful signal
    # without making migration behavior depend on the article's full length.
    clean_body = re.sub(r"(?im)^\s*(?:your prompt|search(?:'s)? response|assistant(?: response)?|user)\s*:\s*$", "", body)
    clean_tags = [tag for tag in tags if not is_provenance_tag(tag)]
    return " " + " ".join((title, folder, *clean_tags, clean_body[:3000])).casefold() + " "


def classify(title: str, tags: Iterable[str], folder: str, body: str = "") -> tuple[Category, float, str]:
    text = _search_text(title, tags, folder, body)
    scores: dict[str, int] = {item.slug: 0 for item in CATEGORIES}
    matched: dict[str, list[str]] = {item.slug: [] for item in CATEGORIES}
    for slug, terms in _RULES:
        for term in terms:
            if term.casefold() in text:
                scores[slug] += 1
                matched[slug].append(term)
    best_slug = max(scores, key=scores.get)
    best_score = scores[best_slug]
    second_score = sorted(scores.values(), reverse=True)[1]
    if best_score == 0:
        chosen = CATEGORY_BY_SLUG["computing"]
        return chosen, 0.2, "No rule matched; assigned to the general computing fallback."
    confidence = min(0.99, 0.55 + min(best_score, 5) * 0.08 + max(0, best_score - second_score) * 0.05)
    reason = "Matched " + ", ".join(matched[best_slug][:5])
    if best_score == second_score:
        confidence = min(confidence, 0.55)
        reason += "; tied with another category"
    return CATEGORY_BY_SLUG[best_slug], round(confidence, 2), reason


def category_from_text(text: str, *, folder: str = "") -> Category:
    title_match = re.search(r"^Title:\s*(?:\"([^\"]*)\"|'([^']*)'|(.*))$", text, re.M)
    title = next((part for part in title_match.groups() if part is not None), "") if title_match else ""
    fm_match = re.match(r"\A---\s*\n(.*?)\n---", text, re.S)
    frontmatter = fm_match.group(1) if fm_match else ""
    body = text[fm_match.end():] if fm_match else text
    return classify(title, parse_tags(frontmatter), folder, body)[0]
