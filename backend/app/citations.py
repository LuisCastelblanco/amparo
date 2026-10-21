"""Deterministic detection and normalization of Colombian Constitutional Court ruling IDs.

Recognizes forms such as T-760/08, T-760 de 2008, T 760/2008, SU-508 de 2020, C-313/14
and the URL slugs used by the Court's relatoria (t-760-08.htm).
"""

import re
from urllib.parse import urlsplit

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


# Relatoria paths: /relatoria/2010/T-378-10.htm, /relatoria/2025/t-234-25, /relatoria/2008/T-760-08.rtf
_RELATORIA_RE = re.compile(
    rf"/relatoria/(?P<folder>\d{{4}})/(?P<type>{_TYPES})-?(?P<num>\d{{1,4}})[A-Za-z]?-(?P<year>\d{{2}}|\d{{4}})"
    r"(?:\.[A-Za-z]{2,5})?/?$",
    re.IGNORECASE,
)
# Types for health tutelas, in priority order. C (constitutionality) rulings are kept only as filler.
TYPE_PRIORITY = {"SU": 0, "T": 0, "C": 1}


def id_from_url(url: str) -> str | None:
    """Ruling ID from a relatoria URL path: /relatoria/2010/T-378-10.htm -> T-378/10.

    Case and file extension are ignored; the slug's year wins over the folder year.
    """
    path = urlsplit(url).path
    m = _RELATORIA_RE.search(path)
    if m is None:
        return None
    if not 1992 <= _full_year(m["year"]) <= 2100:
        return None
    return normalize(m["type"], m["num"], m["year"])


def is_relatoria(url: str) -> bool:
    """A ruling page (/relatoria/<year>/...), not a bulletin or other relatoria document."""
    return re.search(r"/relatoria/\d{4}/", urlsplit(url).path, re.IGNORECASE) is not None


def id_from_source(title: str, url: str) -> str | None:
    """The ruling a search result is about: from the URL path, else (relatoria pages only) the title.

    News items, bulletins and PDFs that merely mention rulings return None.
    """
    from_url = id_from_url(url)
    if from_url is not None or not is_relatoria(url):
        return from_url
    found = find_citations(title)
    return found[0] if found else None


def ruling_type(canonical: str) -> str:
    return canonical.split("-", 1)[0]


def display(canonical: str) -> str:
    """T-760/08 -> T-760 de 2008, for use in the Spanish document."""
    head, yy = canonical.split("/")
    return f"{head} de {_full_year(yy)}"

