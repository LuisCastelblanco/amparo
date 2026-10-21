"""The six-step tutela pipeline: intake -> analysis -> research -> draft -> verify -> package."""

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app import citations, grounding, sources
from app.config import get_settings
from app.llm import ChatResult, Tier, chat
from app.schemas import (
    CaseFacts,
    CitationCheck,
    CitationOutcome,
    CoherenceReview,
    FinalPackage,
    LegalAnalysis,
    PackageGuidance,
    ParagraphRewrite,
    Precedent,
    Precedents,
    RelevanceReview,
    TutelaDraft,
    TutelaSections,
    VerificationReport,
)
from app.search import SearchResult, asearch_jurisprudence

logger = logging.getLogger("amparo.pipeline")

M = TypeVar("M", bound=BaseModel)

MAX_CANDIDATES = 10
MAX_PRECEDENTS = 8
EXCERPT_CHARS = 1200
SCREENING_CHARS = 800
MAX_VERIFY_ITERATIONS = 2
# Output budget per LLM call. Reasoning-tier calls include the model's thinking tokens.
MAX_TOKENS = {
    "intake": 4096,
    "analysis": 12288,
    "screening": 4096,
    "draft": 8192,
    "rewrite": 4096,
    "coherence": 12288,
    "package": 4096,
}
DISCLAIMER = (
    "Amparo es una herramienta de ayuda para la redacción y no constituye asesoría legal. "
    "Revise el documento, complete los datos entre corchetes y, si es posible, consúltelo con un "
    "abogado, un consultorio jurídico universitario o la Defensoría del Pueblo antes de radicarlo."
)
OUTPUT_LANGUAGE = "All user-facing text values in the JSON must be written in Spanish (Colombia)."


# --- Usage tracking ----------------------------------------------------------


class SearchBudgetExceeded(RuntimeError):
    pass


@dataclass
class SearchBudget:
    """Maximum number of Tavily searches for a whole case."""

    remaining: int

    def take(self, n: int) -> int:
        """Reserve up to `n` searches; return how many were granted."""
        granted = max(0, min(n, self.remaining))
        self.remaining -= granted
        return granted


@dataclass
class StepUsage:
    calls: list[ChatResult] = field(default_factory=list)
    tavily_searches: int = 0
    budget: SearchBudget = field(default_factory=lambda: SearchBudget(get_settings().max_tavily_searches_per_case))
    # Sends an intermediate SSE event (escalations, verification iterations) while the step runs.
    emit: Callable[[str, dict[str, Any]], None] = lambda event, data: None
    escalations: list[dict[str, Any]] = field(default_factory=list)

    def metrics(self) -> dict[str, Any]:
        costs = [c.cost_usd for c in self.calls]
        return {
            "models": list(dict.fromkeys(c.model for c in self.calls)),
            "final_model": self.calls[-1].model if self.calls else None,
            "escalations": self.escalations,
            "llm_calls": len(self.calls),
            "input_tokens": sum(c.input_tokens for c in self.calls),
            "output_tokens": sum(c.output_tokens for c in self.calls),
            "llm_latency_s": round(sum(c.latency_s for c in self.calls), 2),
            "cost_usd": round(sum(c for c in costs if c is not None), 6),
            "cost_complete": all(c is not None for c in costs),
            "tavily_searches": self.tavily_searches,
        }


# --- Structured LLM helper ---------------------------------------------------


def strict_schema(model: type[BaseModel]) -> dict:
    """JSON schema for `model` with every object closed and every property required."""
    schema = model.model_json_schema()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return schema


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


async def structured(
    tier: Tier,
    model: type[M],
    system: str,
    user: str,
    usage: StepUsage,
    max_tokens: int,
) -> M:
    """Call the LLM with a JSON schema and parse into `model`, retrying once on invalid output."""
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": model.__name__, "schema": strict_schema(model), "strict": True},
    }
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for attempt in range(2):
        result = await asyncio.to_thread(
            chat, tier, messages, response_format=response_format, max_tokens=max_tokens
        )
        usage.calls.append(result)
        if result.finish_reason == "length":
            raise ValueError(f"{model.__name__}: output truncated at max_tokens={max_tokens} ({result.model})")
        try:
            return model.model_validate_json(_strip_fences(result.content))
        except ValidationError as exc:
            if attempt == 1:
                raise ValueError(f"{model.__name__}: model returned invalid JSON after retry: {exc}") from exc
            logger.warning("invalid %s output, retrying: %s", model.__name__, exc)
            messages += [
                {"role": "assistant", "content": result.content},
                {"role": "user", "content": f"That JSON is invalid: {exc}. Return only valid JSON for the schema."},
            ]
    raise AssertionError("unreachable")


def _dump(model: BaseModel) -> str:
    return json.dumps(model.model_dump(), ensure_ascii=False, indent=2)


# --- Step 1: intake ----------------------------------------------------------

INTAKE_SYSTEM = f"""You extract the facts of a Colombian health-care complaint (a person whose EPS denied or \
delayed a health service) from the person's own narrative, to prepare an acción de tutela.
Rules:
- Use only what the narrative says. Never invent names, dates, diagnoses or documents.
- Copy drug, procedure and diagnosis names exactly as the user wrote them.
- servicio_negado: a short noun phrase (at most 8 words) naming only what was denied, in the user's words.
- eps: the EPS name exactly as written. edad: the age in years if the narrative states it.
- Include every date the user mentions in fechas_relevantes and every document in anexos_mencionados.
- Use null for an unknown age or special condition.
- In datos_faltantes, ask short questions only for information that is critical to draft the tutela \
(e.g. which EPS, what was denied, whether there is a medical prescription). Leave it empty otherwise.
{OUTPUT_LANGUAGE}"""


async def intake(narrative: str, usage: StepUsage) -> CaseFacts:
    """Extract facts with the fast tier; escalate to the draft tier if any fact is not grounded."""
    user = f"Narrative:\n{narrative}"
    facts = await structured("fast", CaseFacts, INTAKE_SYSTEM, user, usage, max_tokens=MAX_TOKENS["intake"])
    issues = grounding.check(facts, narrative)
    if not issues:
        return facts
    escalation = {
        "step": "intake",
        "from_tier": "fast",
        "from_model": usage.calls[-1].model,
        "to_tier": "draft",
        "to_model": get_settings().model_for_tier("draft"),
        "reasons": issues,
    }
    logger.warning("intake not grounded, escalating to draft tier: %s", issues)
    usage.emit("escalated", escalation)
    facts = await structured("draft", CaseFacts, INTAKE_SYSTEM, user, usage, max_tokens=MAX_TOKENS["intake"])
    escalation["remaining_issues"] = grounding.check(facts, narrative)
    usage.escalations.append(escalation)
    return facts


# --- Step 2: legal analysis --------------------------------------------------

ANALYSIS_SYSTEM = f"""You are a Colombian constitutional law analyst. Given the facts of a health-care \
complaint against an EPS, determine:
- the fundamental rights at stake (e.g. salud, vida digna, seguridad social, dignidad humana);
- whether the acción de tutela is admissible (procedencia: subsidiarity, immediacy, standing), with a short reason;
- the urgency (alta / media / baja) given the patient's condition;
- 2 to 4 short Spanish search queries to find relevant Constitutional Court rulings (sentencias de tutela) \
on corteconstitucional.gov.co. Make queries specific to the denied service and the patient's situation.
{OUTPUT_LANGUAGE}"""


async def analysis(narrative: str, facts: CaseFacts, usage: StepUsage) -> LegalAnalysis:
    user = f"Narrative:\n{narrative}\n\nExtracted facts:\n{_dump(facts)}"
    return await structured("reasoning", LegalAnalysis, ANALYSIS_SYSTEM, user, usage, max_tokens=MAX_TOKENS["analysis"])


# --- Step 3: research --------------------------------------------------------

RELEVANCE_SYSTEM = f"""You screen Colombian Constitutional Court rulings found by a search engine for a tutela against an EPS. Mark a ruling as relevant (relevante=true) when its excerpt deals with the same kind of problem (the same or a similar denied or delayed service, the same patient situation) OR with the general rules on the right to health that a judge would apply to this case (oportunidad, integralidad, continuidad, services not covered by the PBS, special protection subjects). Mark it as not relevant only when it is clearly about something else (pensions, labor, other areas of law).
For each ruling, summarize in one or two sentences why it matters for this case, based only on the excerpt.
Return one item per ruling, using exactly the sentencia_id given.
{OUTPUT_LANGUAGE}"""


async def _search_all(queries: list[str], usage: StepUsage) -> list[list[SearchResult]]:
    """Run the queries in parallel (each with its own timeout), within the case's search budget."""
    granted = usage.budget.take(len(queries))
    if granted == 0:
        raise SearchBudgetExceeded("Tavily search budget for this case is exhausted")
    queries = queries[:granted]
    outcomes = await asyncio.gather(*(asearch_jurisprudence(q) for q in queries), return_exceptions=True)
    usage.tavily_searches += len(queries)
    batches = []
    for query, outcome in zip(queries, outcomes):
        if isinstance(outcome, BaseException):
            logger.warning("tavily query failed query=%r error=%r", query, outcome)
            continue
        batches.append(outcome)
    if not batches:
        raise RuntimeError(f"All {len(queries)} Tavily searches failed; see server logs")
    return batches


def _round_robin(batches: list[list[SearchResult]]) -> list[SearchResult]:
    """Interleave results so every query contributes to the candidate pool."""
    longest = max((len(b) for b in batches), default=0)
    return [b[i] for i in range(longest) for b in batches if i < len(b)]


def _candidates(results: list[SearchResult]) -> dict[str, tuple[SearchResult, Precedent]]:
    """One candidate per ruling (ID from the URL path), T and SU before C, at most MAX_CANDIDATES."""
    by_id: dict[str, tuple[SearchResult, Precedent]] = {}
    seen_urls: set[str] = set()
    for result in results:
        url = result.url.split("#")[0]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        sentencia_id = citations.id_from_source(result.title, url)
        if sentencia_id is None or sentencia_id in by_id:
            continue
        by_id[sentencia_id] = (result, Precedent(
            sentencia_id=sentencia_id,
            url=url,
            resumen_relevancia="",
            extracto_fuente=result.content[:EXCERPT_CHARS],
        ))
    # Stable sort keeps the round-robin order within each priority level.
    ranked = sorted(by_id.items(), key=lambda kv: citations.TYPE_PRIORITY.get(citations.ruling_type(kv[0]), 2))
    return dict(ranked[:MAX_CANDIDATES])


async def enrich_from_pages(precedents: list[Precedent]) -> None:
    """Download each ruling page with its real charset; restore the excerpt's accents and list its citations."""
    pages = await sources.fetch_rulings([p.url for p in precedents])
    for p in precedents:
        page = pages.get(p.url)
        if page is None:
            continue
        p.extracto_fuente, p.acentos_restaurados = sources.restore_accents(p.extracto_fuente, page.text)
        p.charset = page.charset
        p.citas_mencionadas = [c for c in page.cited if c != p.sentencia_id]


async def research(facts: CaseFacts, legal: LegalAnalysis, usage: StepUsage) -> Precedents:
    batches = await _search_all(legal.queries_de_busqueda, usage)
    candidates = _candidates(_round_robin(batches))
    if not candidates:
        return Precedents(precedentes=[])

    listing = "\n\n".join(
        f"sentencia_id: {p.sentencia_id}\ntitle: {r.title}\nexcerpt: {p.extracto_fuente[:SCREENING_CHARS]}"
        for r, p in candidates.values()
    )
    user = f"Case facts:\n{_dump(facts)}\n\nRulings:\n{listing}"
    review = await structured(
        "fast", RelevanceReview, RELEVANCE_SYSTEM, user, usage, max_tokens=MAX_TOKENS["screening"]
    )

    reviewed = {item.sentencia_id: item for item in review.items}
    selected = []
    for _, precedent in candidates.values():
        item = reviewed.get(precedent.sentencia_id)
        if item is not None and not item.relevante:
            continue
        if item is not None:
            precedent.resumen_relevancia = item.resumen_relevancia
        selected.append(precedent)
    selected = selected[:MAX_PRECEDENTS]
    await enrich_from_pages(selected)
    return Precedents(precedentes=selected)


# --- Step 4: draft -----------------------------------------------------------

DRAFT_SYSTEM = f"""You draft the sections of a Colombian acción de tutela (Decreto 2591 de 1991) for a person \
whose EPS denied or delayed a health service. The person files it themselves, without a lawyer.
Rules:
- Write in clear, formal Spanish that a non-lawyer can understand and sign.
- hechos: numbered, chronological, first person, only facts from the case. No invented details.
- fundamentos_juridicos: you may cite articles of the Constitución Política (e.g. 11, 48, 49, 86), \
the Decreto 2591 de 1991 and the Ley 1751 de 2015 (Ley Estatutaria de Salud).
- CITATIONS: support the legal grounds with the Constitutional Court rulings in the ALLOWED RULINGS list: \
cite every one that applies (normally 2 to 4), written as "Sentencia T-123 de 2024", with one sentence on what \
each one says, based on its relevance note. Cite ONLY rulings from that list; never any other ruling, even \
well-known ones. If the list is empty, cite no rulings.
- pretensiones: concrete orders (e.g. authorize and deliver the service within 48 hours, comprehensive treatment).
- Use bracket placeholders for personal data you do not have: [NOMBRE COMPLETO], [NÚMERO DE CÉDULA], [CIUDAD].
- juramento: state under oath that no other tutela has been filed for the same facts and rights.
{OUTPUT_LANGUAGE}"""


def _allowed_rulings(precedents: Precedents) -> str:
    if not precedents.precedentes:
        return "(empty: do not cite any ruling)"
    return "\n\n".join(
        f"- Sentencia {citations.display(p.sentencia_id)} ({p.url})\n"
        f"  relevance: {p.resumen_relevancia}\n  excerpt: {p.extracto_fuente[:600]}"
        for p in precedents.precedentes
    )


def _numbered(items: list[str]) -> str:
    # Models often number items themselves; drop that so numbering is not doubled.
    clean = (re.sub(r"^\s*\d+\s*[.)-]\s*", "", item) for item in items)
    return "\n".join(f"{i}. {item}" for i, item in enumerate(clean, 1))


def _right_name(right: str) -> str:
    """'Derecho a la salud' -> 'la salud', so it reads well after 'mis derechos fundamentales a'."""
    return re.sub(r"^(el\s+)?derecho\s+(fundamental\s+)?(a\s+|al\s+)?", "", right.strip(), flags=re.IGNORECASE)


def assemble_tutela(facts: CaseFacts, s: TutelaSections) -> str:
    derechos = ", ".join(_right_name(r) for r in s.derechos_vulnerados)
    return f"""SEÑOR(A) JUEZ DE LA REPÚBLICA (REPARTO)
[CIUDAD]
E. S. D.

REFERENCIA: ACCIÓN DE TUTELA
ACCIONANTE: [NOMBRE COMPLETO], C.C. [NÚMERO DE CÉDULA]
ACCIONADA: {facts.eps}

[NOMBRE COMPLETO], mayor de edad, identificado(a) con cédula de ciudadanía No. [NÚMERO DE CÉDULA], \
domiciliado(a) en [CIUDAD], actuando en nombre propio, presento ACCIÓN DE TUTELA contra {facts.eps}, \
con fundamento en el artículo 86 de la Constitución Política y el Decreto 2591 de 1991, para que se \
protejan mis derechos fundamentales a: {derechos}, con base en lo siguiente:

I. HECHOS

{_numbered(s.hechos)}

II. DERECHOS VULNERADOS

{_numbered(s.derechos_vulnerados)}

III. FUNDAMENTOS JURÍDICOS

{s.fundamentos_juridicos}

IV. PRETENSIONES

{_numbered(s.pretensiones)}

V. PRUEBAS Y ANEXOS

{_numbered(s.pruebas_y_anexos)}

VI. JURAMENTO

{s.juramento}

VII. NOTIFICACIONES

Accionante: [DIRECCIÓN], [TELÉFONO], [CORREO ELECTRÓNICO]
Accionada: {facts.eps}, [DIRECCIÓN DE NOTIFICACIONES JUDICIALES DE LA EPS]

Atentamente,


______________________________
[NOMBRE COMPLETO]
C.C. [NÚMERO DE CÉDULA]
"""


async def draft(
    facts: CaseFacts, legal: LegalAnalysis, precedents: Precedents, usage: StepUsage
) -> TutelaDraft:
    user = (
        f"Case facts:\n{_dump(facts)}\n\nLegal analysis:\n{_dump(legal)}\n\n"
        f"ALLOWED RULINGS:\n{_allowed_rulings(precedents)}"
    )
    budget = MAX_TOKENS["draft"]
    sections = await structured("draft", TutelaSections, DRAFT_SYSTEM, user, usage, max_tokens=budget)
    if precedents.precedentes and not citations.find_citations(sections.fundamentos_juridicos):
        logger.warning("draft cited no allowed ruling; retrying once")
        reminder = (
            "\n\nIMPORTANT: a previous attempt cited no ruling. fundamentos_juridicos must cite the applicable "
            'ALLOWED RULINGS as "Sentencia T-123 de 2024".'
        )
        sections = await structured("draft", TutelaSections, DRAFT_SYSTEM, user + reminder, usage, max_tokens=budget)
    return TutelaDraft(**sections.model_dump(), texto_completo=assemble_tutela(facts, sections))


# --- Step 5: verification ----------------------------------------------------

COHERENCE_SYSTEM = f"""You review a draft acción de tutela for internal coherence. Check that:
- every pretensión is supported by the hechos;
- the hechos match the extracted case facts (no invented facts, dates or documents);
- the derechos vulnerados follow from the hechos.
Do not evaluate citations of rulings; they are checked separately. List concrete issues only.
{OUTPUT_LANGUAGE}"""


REWRITE_SYSTEM = f"""You edit paragraphs of a Colombian acción de tutela. Some rulings it cites could not be \
verified against the Constitutional Court's website and must be removed.
Rules:
- Remove every reference to the rulings listed under REMOVE, including sentences that only describe them.
- Keep the rest of each paragraph's legal argument and wording, and keep every other citation exactly as it is.
- Never add rulings, laws or facts that are not already in the paragraph.
- If a paragraph only presented a removed ruling, return an empty string for it.
- Return exactly one paragraph per input paragraph, in the same order.
{OUTPUT_LANGUAGE}"""

_TEXT_FIELDS = ("hechos", "derechos_vulnerados", "fundamentos_juridicos", "pretensiones", "pruebas_y_anexos", "juramento")


def check_citations(found: list[str], precedents: Precedents) -> tuple[list[CitationCheck], list[CitationCheck]]:
    """Split citations into verified (a found ruling, or cited inside one) and unverified."""
    by_id = {p.sentencia_id: p for p in precedents.precedentes}
    mentioned_in: dict[str, Precedent] = {}
    for p in precedents.precedentes:
        for cited in [*citations.find_citations(p.extracto_fuente), *p.citas_mencionadas]:
            mentioned_in.setdefault(cited, p)

    verified: list[CitationCheck] = []
    unverified: list[CitationCheck] = []
    for sid in found:
        if sid in by_id:
            p = by_id[sid]
            detalle = (
                p.resumen_relevancia
                if p.origen == "busqueda_dirigida"
                else "Coincide con una sentencia encontrada en la búsqueda."
            )
            verified.append(CitationCheck(sentencia_id=sid, estado="verificada", url=p.url, detalle=detalle))
        elif sid in mentioned_in:
            source = mentioned_in[sid]
            verified.append(CitationCheck(
                sentencia_id=sid, estado="verificada", url=source.url,
                detalle=f"Verificación indirecta: aparece citada dentro de la sentencia {source.sentencia_id}.",
            ))
        else:
            unverified.append(CitationCheck(
                sentencia_id=sid, estado="no_verificada",
                detalle="No aparece en las fuentes consultadas.",
            ))
    return verified, unverified


async def targeted_search(pending: list[str], usage: StepUsage) -> dict[str, Precedent]:
    """Search corteconstitucional.gov.co for each ruling by ID; return the ones found, keyed by ID."""
    granted = usage.budget.take(len(pending))
    if granted < len(pending):
        logger.warning("search budget allows %d of %d targeted searches", granted, len(pending))
    searched = pending[:granted]
    if not searched:
        return {}
    # "sentencia T-760/08 relatoria" finds the ruling page; "Sentencia T-760 de 2008" mostly returns the
    # follow-up autos of famous rulings.
    outcomes = await asyncio.gather(
        *(asearch_jurisprudence(f"sentencia {sid} relatoria") for sid in searched),
        return_exceptions=True,
    )
    usage.tavily_searches += len(searched)
    found: dict[str, Precedent] = {}
    for sid, outcome in zip(searched, outcomes):
        if isinstance(outcome, BaseException):
            logger.warning("targeted search failed id=%s error=%r", sid, outcome)
            continue
        exact = next((r for r in outcome if citations.id_from_url(r.url.split("#")[0]) == sid), None)
        # Otherwise, an official Court document whose text cites the ruling proves that it exists.
        cited_in = next((r for r in outcome if sid in citations.find_citations(f"{r.title} {r.content}")), None)
        match = exact or cited_in
        if match is None:
            continue
        note = (
            "Encontrada con una búsqueda dirigida durante la verificación de citas."
            if exact
            else "Una búsqueda dirigida encontró un documento oficial de la Corte que la cita."
        )
        found[sid] = Precedent(
            sentencia_id=sid,
            url=match.url.split("#")[0],
            resumen_relevancia=note,
            extracto_fuente=match.content[:EXCERPT_CHARS],
            origen="busqueda_dirigida",
        )
    await enrich_from_pages([p for p in found.values() if citations.id_from_url(p.url)])
    return found


def _units(sections: TutelaSections) -> dict[str, list[str]]:
    """Each text field as a list of editable units: list items, or the lines of a text field."""
    return {
        name: list(value) if isinstance(value := getattr(sections, name), list) else value.split("\n")
        for name in _TEXT_FIELDS
    }


def _rebuild(sections: TutelaSections, units: dict[str, list[str]]) -> TutelaSections:
    data: dict[str, Any] = {}
    for name in _TEXT_FIELDS:
        if isinstance(getattr(sections, name), list):
            data[name] = [u for u in units[name] if u.strip()]
        else:
            data[name] = re.sub(r"\n{3,}", "\n\n", "\n".join(units[name])).strip()
    return TutelaSections(**data)


def _cites_any(text: str, flagged: set[str]) -> bool:
    return bool(set(citations.find_citations(text)) & flagged)


def strip_sentences(text: str, flagged: set[str]) -> str:
    """Deterministic last resort: drop the sentences that cite a flagged ruling."""
    sentences = re.split(r"(?<=[.;])\s+", text)
    return " ".join(s for s in sentences if not _cites_any(s, flagged))


async def rewrite_without(
    facts: CaseFacts, tutela: TutelaDraft, flagged: set[str], usage: StepUsage
) -> TutelaDraft:
    """Rewrite only the paragraphs that cite a flagged ruling, with the draft tier, without those citations."""
    sections = TutelaSections(**tutela.model_dump(include=set(_TEXT_FIELDS)))
    units = _units(sections)
    targets = [(name, i) for name, items in units.items() for i, item in enumerate(items) if _cites_any(item, flagged)]
    if not targets:
        return tutela
    original = [units[name][i] for name, i in targets]
    user = (
        f"REMOVE: {', '.join('Sentencia ' + citations.display(s) for s in sorted(flagged))}\n\n"
        f"Paragraphs (JSON list):\n{json.dumps(original, ensure_ascii=False, indent=2)}"
    )
    try:
        rewrite = await structured(
            "draft", ParagraphRewrite, REWRITE_SYSTEM, user, usage, max_tokens=MAX_TOKENS["rewrite"]
        )
        rewritten = rewrite.parrafos if len(rewrite.parrafos) == len(original) else None
    except ValueError as exc:
        logger.warning("paragraph rewrite failed: %s", exc)
        rewritten = None
    if rewritten is None:
        logger.warning("paragraph rewrite unusable; removing the sentences deterministically")
        rewritten = original
    for (name, i), old, new in zip(targets, original, rewritten):
        # Never keep a flagged citation, even if the model left one behind.
        units[name][i] = strip_sentences(new, flagged) if _cites_any(new, flagged) else new
    sections = _rebuild(sections, units)
    return TutelaDraft(**sections.model_dump(), texto_completo=assemble_tutela(facts, sections))


async def verify(
    facts: CaseFacts, tutela: TutelaDraft, precedents: Precedents, usage: StepUsage
) -> tuple[TutelaDraft, VerificationReport, Precedents]:
    """Verify every citation; search for the missing ones, rewrite without the ones that do not exist.

    Loop (at most MAX_VERIFY_ITERATIONS): targeted Tavily search per unverified ruling; found ones
    join the precedents, the rest are removed by rewriting only the affected paragraphs. Anything
    still unverified after the loop is removed deterministically, so the final text has none.
    """
    precedents = precedents.model_copy(deep=True)
    verified, unverified = check_citations(citations.find_citations(tutela.texto_completo), precedents)
    initial_unverified = [c.sentencia_id for c in unverified]
    outcomes: list[CitationOutcome] = []

    iteration = 0
    while unverified and iteration < MAX_VERIFY_ITERATIONS:
        iteration += 1
        pending = [c.sentencia_id for c in unverified]
        usage.emit("verify_iteration", {"step": "verify", "iteration": iteration, "phase": "searching",
                                        "unverified": pending})
        found = await targeted_search(pending, usage)
        for sid, p in found.items():
            precedents.precedentes.append(p)
            outcomes.append(CitationOutcome(
                sentencia_id=sid, resultado="verificada_busqueda_dirigida", iteracion=iteration, url=p.url,
                detalle="Se encontró en corteconstitucional.gov.co con una búsqueda dirigida.",
            ))
        removed = [sid for sid in pending if sid not in found]
        if removed:
            tutela = await rewrite_without(facts, tutela, set(removed), usage)
            outcomes += [
                CitationOutcome(
                    sentencia_id=sid, resultado="retirada_reescritura", iteracion=iteration,
                    detalle="No se encontró en corteconstitucional.gov.co; el párrafo se reescribió sin esta cita.",
                )
                for sid in removed
            ]
        verified, unverified = check_citations(citations.find_citations(tutela.texto_completo), precedents)
        usage.emit("verify_iteration", {
            "step": "verify", "iteration": iteration, "phase": "done", "found": list(found), "removed": removed,
            "still_unverified": [c.sentencia_id for c in unverified],
        })

    if unverified:
        flagged = {c.sentencia_id for c in unverified}
        logger.warning("citations still unverified after %d iterations, removing: %s", iteration, flagged)
        sections = TutelaSections(**tutela.model_dump(include=set(_TEXT_FIELDS)))
        units = {name: [strip_sentences(u, flagged) for u in items] for name, items in _units(sections).items()}
        sections = _rebuild(sections, units)
        tutela = TutelaDraft(**sections.model_dump(), texto_completo=assemble_tutela(facts, sections))
        outcomes += [
            CitationOutcome(
                sentencia_id=sid, resultado="retirada_automatica", iteracion=iteration,
                detalle="Seguía sin verificarse al terminar el ciclo; se retiraron las frases que la citaban.",
            )
            for sid in sorted(flagged)
        ]
        verified, unverified = check_citations(citations.find_citations(tutela.texto_completo), precedents)

    found_final = citations.find_citations(tutela.texto_completo)
    observaciones: list[str] = []
    if initial_unverified:
        recovered = sum(o.resultado == "verificada_busqueda_dirigida" for o in outcomes)
        observaciones.append(
            f"{len(initial_unverified)} cita(s) no se verificaron al principio: {recovered} se encontraron con una "
            f"búsqueda dirigida y {len(initial_unverified) - recovered} se retiraron del texto."
        )
    if not found_final:
        observaciones.append("El borrador no cita sentencias de la Corte Constitucional.")

    sections_json = json.dumps(tutela.model_dump(exclude={"texto_completo"}), ensure_ascii=False, indent=2)
    user = f"Case facts:\n{_dump(facts)}\n\nDraft sections:\n{sections_json}"
    review = await structured(
        "reasoning", CoherenceReview, COHERENCE_SYSTEM, user, usage, max_tokens=MAX_TOKENS["coherence"]
    )
    observaciones += review.observaciones

    report = VerificationReport(
        citas_encontradas=found_final,
        citas_verificadas=verified,
        citas_no_verificadas=unverified,
        citas_no_verificadas_iniciales=initial_unverified,
        ciclo=outcomes,
        iteraciones=iteration,
        observaciones=observaciones,
    )
    return tutela, report, precedents


# --- Step 6: package ---------------------------------------------------------

PACKAGE_SYSTEM = f"""You prepare practical filing guidance for a person in Colombia who will file an acción de \
tutela against their EPS by themselves. Provide:
- checklist_anexos: the documents to attach (ID copy, prescriptions, EPS denial, medical history, etc.), \
based on the case and the draft's annexes;
- donde_radicar: where and how to file (any judge of the place where the violation occurs, oficina judicial / \
reparto, online filing through the Rama Judicial's virtual filing service, no lawyer needed, the judge has \
10 business days to decide; mention an interim measure (medida provisional) when urgency is high);
- que_hacer_si_no_cumplen: what to do if the EPS does not comply (incidente de desacato before the same judge).
Keep it short, concrete and in plain language.
{OUTPUT_LANGUAGE}"""


async def package(
    facts: CaseFacts,
    legal: LegalAnalysis,
    tutela: TutelaDraft,
    report: VerificationReport,
    usage: StepUsage,
) -> FinalPackage:
    user = (
        f"Case facts:\n{_dump(facts)}\n\nUrgency: {legal.urgencia}\n\n"
        f"Draft annexes:\n{_numbered(tutela.pruebas_y_anexos)}"
    )
    guidance = await structured(
        "fast", PackageGuidance, PACKAGE_SYSTEM, user, usage, max_tokens=MAX_TOKENS["package"]
    )
    return FinalPackage(**guidance.model_dump(), draft=tutela, verification=report, aviso=DISCLAIMER)


# --- Orchestration -----------------------------------------------------------

STEPS = ("intake", "analysis", "research", "draft", "verify", "package")


async def run_case(narrative: str) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Run all steps, yielding (event, payload) tuples for each step and the final result."""
    state: dict[str, Any] = {}
    totals = {
        "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "latency_s": 0.0, "tavily_searches": 0,
        "escalations": 0,
    }

    async def step_intake(u: StepUsage) -> BaseModel:
        state["facts"] = await intake(narrative, u)
        return state["facts"]

    async def step_analysis(u: StepUsage) -> BaseModel:
        state["legal"] = await analysis(narrative, state["facts"], u)
        return state["legal"]

    async def step_research(u: StepUsage) -> BaseModel:
        state["precedents"] = await research(state["facts"], state["legal"], u)
        return state["precedents"]

    async def step_draft(u: StepUsage) -> BaseModel:
        state["draft"] = await draft(state["facts"], state["legal"], state["precedents"], u)
        return state["draft"]

    async def step_verify(u: StepUsage) -> BaseModel:
        state["draft"], state["report"], state["precedents"] = await verify(
            state["facts"], state["draft"], state["precedents"], u
        )
        return state["report"]

    async def step_package(u: StepUsage) -> BaseModel:
        state["package"] = await package(state["facts"], state["legal"], state["draft"], state["report"], u)
        return state["package"]

    runners: dict[str, Callable[[StepUsage], Awaitable[BaseModel]]] = {
        "intake": step_intake,
        "analysis": step_analysis,
        "research": step_research,
        "draft": step_draft,
        "verify": step_verify,
        "package": step_package,
    }

    budget = SearchBudget(get_settings().max_tavily_searches_per_case)
    for index, name in enumerate(STEPS, 1):
        yield "step_started", {"step": name, "index": index, "total": len(STEPS)}
        events: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        usage = StepUsage(budget=budget, emit=lambda event, data: events.put_nowait((event, data)))
        start = time.perf_counter()
        task = asyncio.create_task(runners[name](usage))
        try:
            # Forward intermediate events (escalations, verification iterations) while the step runs.
            while not task.done():
                getter = asyncio.create_task(events.get())
                await asyncio.wait({task, getter}, return_when=asyncio.FIRST_COMPLETED)
                if getter.done():
                    yield getter.result()
                else:
                    getter.cancel()
            while not events.empty():
                yield events.get_nowait()
            result = task.result()
        except Exception as exc:  # noqa: BLE001 - reported to the client as step_failed
            logger.exception("step %s failed", name)
            metrics = usage.metrics() | {"latency_s": round(time.perf_counter() - start, 2)}
            _accumulate(totals, metrics)
            yield "step_failed", {"step": name, "index": index, "error": _error_message(exc), "metrics": metrics}
            yield "case_failed", {"failed_step": name, "totals": _round(totals)}
            return
        finally:
            # The client may disconnect mid-step: do not leave the step running in the background.
            if not task.done():
                task.cancel()
        metrics = usage.metrics() | {"latency_s": round(time.perf_counter() - start, 2)}
        _accumulate(totals, metrics)
        yield "step_completed", {"step": name, "index": index, "result": result.model_dump(), "metrics": metrics}

    yield "case_completed", {"package": state["package"].model_dump(), "totals": _round(totals)}


def _accumulate(totals: dict[str, Any], metrics: dict[str, Any]) -> None:
    for key in ("input_tokens", "output_tokens", "cost_usd", "latency_s", "tavily_searches"):
        totals[key] += metrics[key]
    totals["escalations"] += len(metrics["escalations"])


def _round(totals: dict[str, Any]) -> dict[str, Any]:
    return totals | {"cost_usd": round(totals["cost_usd"], 6), "latency_s": round(totals["latency_s"], 2)}


def _error_message(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}"
    return message if len(message) <= 500 else message[:497] + "..."
