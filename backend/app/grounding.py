"""Deterministic check that the facts extracted at intake are grounded in the user's narrative.

The denied service, the EPS and every extracted date must appear in the narrative, comparing
without case or accents and understanding the usual Spanish date formats. Anything that does
not appear is reported as an issue, which makes the pipeline escalate intake to a larger model.
"""

import re
import unicodedata
from typing import NamedTuple

from app.schemas import CaseFacts

MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
_MONTH_RE = "|".join(MONTHS)
_STOPWORDS = {
    "a", "al", "con", "de", "del", "el", "en", "la", "las", "lo", "los", "mi", "mis", "para", "por", "que",
    "se", "su", "sus", "un", "una", "y", "o", "e", "le", "les", "me", "eps",
}
# A paraphrase is accepted when all its content words appear in the narrative, up to this length;
# longer values must appear verbatim (long word salads are a common small-model failure).
MAX_PARAPHRASE_WORDS = 6


class Date(NamedTuple):
    day: int | None
    month: int | None
    year: int | None


def fold(text: str) -> str:
    """Lowercase and strip accents."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", fold(text))


def _phrase(text: str) -> str:
    return " ".join(_words(text))


def parse_dates(text: str) -> list[Date]:
    """All dates in `text`: 2026-08-25, 25/08/2026, 25 de agosto de 2026, agosto de 2026, junio, 2026."""
    t = fold(text)
    found: list[Date] = []
    spans: list[tuple[int, int]] = []

    def add(m: re.Match, date: Date) -> None:
        if any(s <= m.start() < e for s, e in spans):
            return
        spans.append(m.span())
        found.append(date)

    for m in re.finditer(r"\b(\d{4})-(\d{1,2})(?:-(\d{1,2}))?\b", t):
        add(m, Date(int(m[3]) if m[3] else None, int(m[2]), int(m[1])))
    for m in re.finditer(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})\b", t):
        year = int(m[3]) if len(m[3]) == 4 else 2000 + int(m[3])
        add(m, Date(int(m[1]), int(m[2]), year))
    for m in re.finditer(
        rf"\b(?:(\d{{1,2}})\s+(?:de\s+)?)?({_MONTH_RE})(?:\s+(?:de|del)\s+(?:ano\s+)?(\d{{4}}))?\b", t
    ):
        add(m, Date(int(m[1]) if m[1] else None, MONTHS[m[2]], int(m[3]) if m[3] else None))
    for m in re.finditer(r"\b(19\d{2}|20\d{2})\b", t):
        add(m, Date(None, None, int(m[1])))
    return found


def date_grounded(value: str, narrative: str) -> bool:
    parsed = parse_dates(value)
    if not parsed:
        # Free-text dates ("hace dos semanas") must appear verbatim.
        return _phrase(value) in _phrase(narrative)
    v = parsed[0]
    mentions = parse_dates(narrative)
    years = {m.year for m in mentions if m.year}
    for m in mentions:
        if v.day is not None and v.day != m.day:
            continue
        if v.month is not None and v.month != m.month:
            continue
        # A year the narrative leaves implicit ("el 10 de agosto") may be inferred from another date.
        if v.year is not None and v.year != m.year and not (m.year is None and v.year in years):
            continue
        if v.day is None and v.month is None and m.year != v.year:
            continue
        return True
    return False


def text_grounded(value: str, narrative: str) -> bool:
    phrase = _phrase(value)
    if not phrase:
        return False
    narrative_phrase = _phrase(narrative)
    if phrase in narrative_phrase:
        return True
    content = [w for w in _words(value) if w not in _STOPWORDS]
    vocabulary = set(_words(narrative))
    return 0 < len(content) <= MAX_PARAPHRASE_WORDS and all(w in vocabulary for w in content)


def check(facts: CaseFacts, narrative: str) -> list[str]:
    """Issues (in English, for logs and the UI timeline); empty when every checked fact is grounded."""
    issues = []
    if not text_grounded(facts.servicio_negado, narrative):
        issues.append(f"denied service not found in the narrative: {facts.servicio_negado!r}")
    if not text_grounded(facts.eps, narrative):
        issues.append(f"EPS not found in the narrative: {facts.eps!r}")
    for item in facts.fechas_relevantes:
        if not date_grounded(item.fecha, narrative):
            issues.append(f"date not found in the narrative: {item.fecha!r}")
    return issues
