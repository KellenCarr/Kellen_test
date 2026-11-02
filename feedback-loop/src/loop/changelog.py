"""Parse a CHANGELOG.md ("## <version>" then "- <entry>" lines) and an Atom release feed."""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime

_VERSION = re.compile(r"^## (\S+)\s*$")
_ENTRY = re.compile(r"^- (.+)$")
_SURFACE_TAG = re.compile(r"^\[([^\]]+)\]\s*")
CHANGE_TYPES = {"added", "fixed", "improved", "changed", "removed", "deprecated", "updated", "reduced"}
_VERB_STEMS = {"fix": "fixed", "add": "added", "improve": "improved", "change": "changed", "remove": "removed"}
_OS_PREFIX = re.compile(r"^(Windows|macOS|Linux|WSL2?)(?:/\w+)?:\s*")
# "Self-hosted runner: Improved ...": a short label, a colon, then a change verb.
_SURFACE_PREFIX = re.compile(
    r"^([A-Z][\w+.-]*(?:[ /][\w+.-]+){0,3}):\s+(?=(?:" + "|".join(t.title() for t in CHANGE_TYPES) + r")\b)"
)
_SURFACE_ALIASES = {"vs_code": "vscode", "vscode_extension": "vscode"}
_ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class ChangelogEntry:
    version: str
    position: int  # order within the version, 0-based
    change_type: str  # fixed | added | improved | ... | other
    surface: str | None  # e.g. 'vscode', 'cloud_sessions', 'windows'; None when untagged
    text: str


def _slug(s: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return _SURFACE_ALIASES.get(slug, slug)


def parse_changelog(md: str) -> list[ChangelogEntry]:
    entries: list[ChangelogEntry] = []
    version, position = None, 0
    for line in md.splitlines():
        if m := _VERSION.match(line):
            version, position = m.group(1), 0
            continue
        m = _ENTRY.match(line)
        if not (m and version):
            continue
        text = m.group(1).strip()
        rest, surface = text, None
        if t := _SURFACE_TAG.match(rest):
            surface, rest = _slug(t.group(1)), rest[t.end():]
        elif p := (_OS_PREFIX.match(rest) or _SURFACE_PREFIX.match(rest)):
            surface, rest = _slug(p.group(1)), rest[p.end():]
        first = rest.split(" ", 1)[0].lower().rstrip(":")
        first = _VERB_STEMS.get(first, first)
        entries.append(ChangelogEntry(version, position, first if first in CHANGE_TYPES else "other", surface, text))
        position += 1
    return entries


def parse_release_feed(xml: str) -> dict[str, datetime]:
    """Map version -> release time from an Atom feed whose entry titles end in 'v<version>'."""
    root = ET.fromstring(xml)
    out = {}
    for entry in root.iter(f"{_ATOM}entry"):
        title = entry.findtext(f"{_ATOM}title") or ""
        updated = entry.findtext(f"{_ATOM}updated")
        m = re.search(r"v(\d[\w.\-]*)\s*$", title)
        if m and updated:
            out[m.group(1)] = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    return out
