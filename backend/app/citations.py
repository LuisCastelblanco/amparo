"""Deterministic detection and normalization of Colombian Constitutional Court ruling IDs.

Recognizes forms such as T-760/08, T-760 de 2008, T 760/2008, SU-508 de 2020, C-313/14
and the URL slugs used by the Court's relatoria (t-760-08.htm).
"""

import re

_TYPES = r"SU|T|C"
_CITATION_RE = re.compile(
    rf"\b(?P<type>{_TYPES})\s*[-‐–—]?\s*(?P<num>\d{{1,4}})[A-Za-z]?"
    rf"\s*(?:/|-|\s+de(?:l\s+año)?\s+)\s*(?P<year>\d{{4}}|\d{{2}})\b",
    re.IGNORECASE,
)


def _full_year(year: str) -> int:
    value = int(year)
    if len(year) == 4:
        return value
    return 1900 + value if value >= 92 else 2000 + value


def normalize(type_: str, num: str, year: str) -> str:
    """Canonical form: T-760/08 (number zero-padded to 3 digits, as the Court does: T-025/04)."""
    return f"{type_.upper()}-{int(num):03d}/{_full_year(year) % 100:02d}"


def find_citations(text: str) -> list[str]:
    """Return unique canonical ruling IDs in order of first appearance."""
    seen: dict[str, None] = {}
    for m in _CITATION_RE.finditer(text):
        year = _full_year(m["year"])
        if not 1992 <= year <= 2100:
            continue
        seen.setdefault(normalize(m["type"], m["num"], m["year"]))
    return list(seen)


def id_from_source(title: str, url: str) -> str | None:
    """Extract the ruling ID a search result is about, preferring the URL slug."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    for candidate in (slug, title):
        found = find_citations(candidate)
        if found:
            return found[0]
    return None


def display(canonical: str) -> str:
    """T-760/08 -> T-760 de 2008, for use in the Spanish document."""
    head, yy = canonical.split("/")
    return f"{head} de {_full_year(yy)}"


def annotate(text: str, flagged: set[str], marker: str) -> str:
    """Append `marker` after every citation in `text` whose canonical ID is in `flagged`."""

    def repl(m: re.Match) -> str:
        if normalize(m["type"], m["num"], m["year"]) in flagged:
            return f"{m[0]} {marker}"
        return m[0]

    return _CITATION_RE.sub(repl, text)
