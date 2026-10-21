"""Deterministic check that the facts extracted at intake are grounded in the user's narrative.

Proper names (the EPS, the city, drug names and codes inside the denied service) and dates must
match exactly, comparing without case or accents and understanding the usual Spanish date formats.
Descriptive fields (the denied service, the patient's special condition) may be paraphrased: enough
of their content words must appear in the narrative, in some form ("cubre" / "cubriera").
Anything not grounded is reported as an issue, which makes the pipeline escalate intake to a
larger model.
"""

import re
import unicodedata
from typing import NamedTuple

from app.config import get_settings
from app.schemas import CaseFacts

MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
_MONTH_RE = "|".join(MONTHS)
_STOPWORDS = {
    "a", "al", "con", "de", "del", "el", "en", "la", "las", "lo", "los", "mi", "mis", "para", "por", "que",
    "se", "su", "sus", "un", "una", "y", "o", "e", "le", "les", "me", "eps", "sin", "no",
}
# A value made only of these says nothing ("nada", "eso") even if the narrative contains it.
_VAGUE = {"nada", "algo", "eso", "esto", "todo", "ninguno", "ninguna", "nulo", "null", "none"}
# Words that frame a descriptive field ("cobertura de transporte", "autorizacion de la cirugia")
# instead of naming what was denied; they need not appear in the narrative.
FRAMING = frozenset({
    "cobertura", "cubrir", "autorizacion", "autorizar", "suministro", "suministrar", "entrega", "entregar",
    "servicio", "servicios", "solicitud", "negacion", "negativa", "programacion", "programar", "realizacion",
    "realizar", "pago", "costo", "costos", "gastos", "paciente", "tratamiento", "procedimiento", "atencion",
})
# Legal categories for the patient's condition, often inferred ("adulto mayor" from "76 anos").
CATEGORIES = frozenset({
    "adulto", "mayor", "persona", "sujeto", "especial", "proteccion", "constitucional", "condicion",
    "situacion", "vulnerable", "vulnerabilidad", "enfermedad", "cronica", "catastrofica", "edad", "avanzada",
    "discapacidad", "tercera", "reforzada", "economica", "recursos",
})
# Drug-name endings: such words are proper names and must match exactly.
_DRUG_RE = re.compile(r"(mab|nib|cept|parina|cilina|micina|pril|sartan|statina|olol|azol|prazol|vir|tida)$")
# Above this many content words a value must appear verbatim (long word salads are a small-model failure).
MAX_PARAPHRASE_WORDS = 12


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


def _same_word(word: str, other: str) -> bool:
    """Other forms of the same word ("cubre" / "cubriera"): they differ only in a short ending."""
    if word == other:
        return True
    common = 0
    for x, y in zip(word, other):
        if x != y:
            break
        common += 1
    return common >= max(4, max(len(word), len(other)) - 4, min(len(word), len(other)) - 2)


def _proper_words(value: str) -> set[str]:
    """Words of a descriptive value that are names: with digits, capitalized after the first word, or drugs."""
    names = set()
    for i, raw in enumerate(re.findall(r"[^\W_]+", value)):
        word = fold(raw)
        if any(c.isdigit() for c in word) or (i > 0 and raw[0].isupper()) or _DRUG_RE.search(word):
            names.add(word)
    return names - _STOPWORDS


def name_grounded(value: str, narrative: str) -> bool:
    """Exact (normalized) match for proper names: the EPS, the city."""
    phrase = _phrase(value)
    if not phrase:
        return False
    if phrase in _phrase(narrative):
        return True
    words = [w for w in _words(value) if w not in _STOPWORDS]
    vocabulary = set(_words(narrative))
    return bool(words) and all(w in vocabulary for w in words)


def overlap(value: str, narrative: str, ignore: frozenset[str] = frozenset()) -> float:
    """Share of the value's content words that appear in the narrative, in some form."""
    content = [w for w in _words(value) if w not in _STOPWORDS and w not in ignore]
    if not content:
        return 1.0
    vocabulary = set(_words(narrative))
    return sum(any(_same_word(w, v) for v in vocabulary) for w in content) / len(content)


def text_grounded(
    value: str, narrative: str, ignore: frozenset[str] = frozenset(), min_overlap: float | None = None
) -> bool:
    """Approximate match for descriptive fields; names inside them must still match exactly."""
    phrase = _phrase(value)
    if not phrase or all(w in _STOPWORDS or w in _VAGUE for w in _words(value)):
        return False
    if phrase in _phrase(narrative):
        return True
    if not _proper_words(value) <= set(_words(narrative)):
        return False
    content = [w for w in _words(value) if w not in _STOPWORDS and w not in ignore]
    if len(content) > MAX_PARAPHRASE_WORDS:
        return False
    threshold = get_settings().grounding_min_overlap if min_overlap is None else min_overlap
    return overlap(value, narrative, ignore) >= threshold


def drop_ungrounded_optional(facts: CaseFacts, narrative: str) -> tuple[CaseFacts, list[str]]:
    """Set the optional facts (city, special condition) that are not grounded to null.

    Small models often invent them (a capital city, an unrelated condition); nulling them is free and
    safe (the city only selects the filing channel), so they do not trigger an escalation.
    """
    dropped = []
    if facts.ciudad and not name_grounded(facts.ciudad, narrative):
        dropped.append(f"city not found in the narrative, ignored: {facts.ciudad!r}")
        facts = facts.model_copy(update={"ciudad": None})
    situation = facts.situacion_del_paciente
    if situation.condicion_especial and not text_grounded(
        situation.condicion_especial, narrative, CATEGORIES | FRAMING, get_settings().grounding_min_overlap_condition
    ):
        dropped.append(f"special condition not found in the narrative, ignored: {situation.condicion_especial!r}")
        situation = situation.model_copy(update={"condicion_especial": None})
        facts = facts.model_copy(update={"situacion_del_paciente": situation})
    return facts, dropped


def check(facts: CaseFacts, narrative: str) -> list[str]:
    """Issues with the core facts (in English, for logs and the UI timeline); empty when all are grounded."""
    issues = []
    if not text_grounded(facts.servicio_negado, narrative, ignore=FRAMING):
        issues.append(f"denied service not found in the narrative: {facts.servicio_negado!r}")
    if not name_grounded(facts.eps, narrative):
        issues.append(f"EPS not found in the narrative: {facts.eps!r}")
    for item in facts.fechas_relevantes:
        if not date_grounded(item.fecha, narrative):
            issues.append(f"date not found in the narrative: {item.fecha!r}")
    return issues
