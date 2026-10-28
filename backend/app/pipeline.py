"""The six-step tutela pipeline: intake -> analysis -> research -> draft -> verify -> package."""

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app import citations, document, grounding, sources
from app.config import get_settings
from app.llm import ChatResult, Tier, chat, estimate_cost
from app.schemas import (
    CaseFacts,
    ChannelExtraction,
    CitationCheck,
    CitationOutcome,
    ClarifyingQuestions,
    CoherenceReview,
    FilingChannel,
    FinalPackage,
    LegalAnalysis,
    NextSteps,
    PackageGuidance,
    Procedencia,
    ParagraphRewrite,
    Precedent,
    Precedents,
    RelevanceReview,
    ReviewerRuling,
    ReviewerSummary,
    TutelaDraft,
    TutelaSections,
    VerificationReport,
)
from app.search import JURISPRUDENCE_DOMAINS, OFFICIAL_DOMAINS, Depth, Searcher, SearchResult, SearchUnavailable, is_official

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
    "channel": 2048,
    "summary": 3072,
    "questions": 1024,
}
DISCLAIMER = (
    "Amparo es una herramienta de ayuda para la redacción y no constituye asesoría legal. "
    "Revise el documento, complete los datos entre corchetes y, si es posible, consúltelo con un "
    "abogado, un consultorio jurídico universitario o la Defensoría del Pueblo antes de radicarlo."
)
OUTPUT_LANGUAGE = "All user-facing text values in the JSON must be written in Spanish (Colombia)."
# Shown verbatim by the UI when a case stops before drafting because there is no case law to cite.
UNAVAILABLE_MESSAGES = {
    "search_unavailable": "Case-law search is not available right now, so Amparo did not draft a tutela: it never "
    "drafts one without verified Constitutional Court rulings. Please try again later, or watch a recorded "
    "sample run.",
    "no_case_law": "The case-law search found no Constitutional Court rulings relevant to this story, so Amparo "
    "did not draft a tutela: it never drafts one without verified rulings. Try describing the denied service "
    "in more detail, or watch a recorded sample run.",
}


# --- Usage tracking ----------------------------------------------------------


class SearchBudgetExceeded(RuntimeError):
    pass


class NoCaseLaw(RuntimeError):
    """Research ended without a single ruling to cite; the case stops before drafting."""

    def __init__(self, reason: str) -> None:
        super().__init__(UNAVAILABLE_MESSAGES[reason])
        self.reason = reason


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
    # Tavily searches answered live (and the credits they cost) and from the cache (free).
    tavily_live: int = 0
    tavily_cached: int = 0
    tavily_credits: int = 0
    budget: SearchBudget = field(default_factory=lambda: SearchBudget(get_settings().max_tavily_searches_per_case))
    searcher: Searcher = field(default_factory=Searcher)
    # Sends an intermediate SSE event (escalations, verification iterations) while the step runs.
    emit: Callable[[str, dict[str, Any]], None] = lambda event, data: None
    escalations: list[dict[str, Any]] = field(default_factory=list)
    # Deterministic corrections made without an LLM call (e.g. an invented city set to null).
    notes: list[str] = field(default_factory=list)
    # Model of the step's main output when calls run in parallel (otherwise the last call's model).
    resolved_by: str | None = None

    async def search(self, query: str, domains: list[str], depth: Depth) -> list[SearchResult]:
        found = await self.searcher.search(query, domains=domains, depth=depth)
        if found.cached:
            self.tavily_cached += 1
        else:
            self.tavily_live += 1
            self.tavily_credits += found.credits
        return found.results

    def metrics(self) -> dict[str, Any]:
        settings = get_settings()
        costs = [c.cost_usd for c in self.calls]
        # Counterfactual: the same calls, with the same token counts, priced at the reasoning tier.
        reasoning = settings.model_for_tier("reasoning")
        all_reasoning = [estimate_cost(reasoning, c.input_tokens, c.output_tokens) for c in self.calls]
        llm_cost = sum(c for c in costs if c is not None)
        search_cost = self.tavily_credits * settings.tavily_usd_per_credit
        return {
            "models": list(dict.fromkeys(c.model for c in self.calls)),
            "final_model": self.resolved_by or (self.calls[-1].model if self.calls else None),
            "escalations": self.escalations,
            "notes": self.notes,
            "llm_calls": len(self.calls),
            "input_tokens": sum(c.input_tokens for c in self.calls),
            "output_tokens": sum(c.output_tokens for c in self.calls),
            "llm_latency_s": round(sum(c.latency_s for c in self.calls), 2),
            # cost_usd = models (llm_cost_usd) + Tavily (search_cost_usd).
            "cost_usd": round(llm_cost + search_cost, 6),
            "llm_cost_usd": round(llm_cost, 6),
            "search_cost_usd": round(search_cost, 6),
            "cost_complete": all(c is not None for c in costs),
            "tiers": list(dict.fromkeys(c.tier for c in self.calls)),
            # Models only: compare with llm_cost_usd.
            "cost_all_reasoning_usd": round(sum(c for c in all_reasoning if c is not None), 6),
            "tavily_searches": self.tavily_live + self.tavily_cached,
            "tavily_live": self.tavily_live,
            "tavily_cached": self.tavily_cached,
            "tavily_credits": self.tavily_credits,
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


async def structured_fast(
    step: str, model: type[M], system: str, user: str, usage: StepUsage, max_tokens: int
) -> M:
    """`structured` on the fast tier; if its output is truncated or invalid, escalate the call to the draft tier."""
    try:
        return await structured("fast", model, system, user, usage, max_tokens=max_tokens)
    except ValueError as exc:
        settings = get_settings()
        reason = str(exc) if len(str(exc)) <= 200 else str(exc)[:197] + "..."
        escalation = {
            "step": step, "from_tier": "fast", "from_model": settings.model_for_tier("fast"),
            "to_tier": "draft", "to_model": settings.model_for_tier("draft"),
            "reasons": [f"unusable output from the fast model: {reason}"],
        }
        logger.warning("fast tier output unusable in %s, escalating: %s", step, reason)
        usage.emit("escalated", escalation)
        result = await structured("draft", model, system, user, usage, max_tokens=max_tokens)
        escalation["remaining_issues"] = []
        usage.escalations.append(escalation)
        return result


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
- ciudad: just the name of the municipality where the person lives (e.g. "Sogamoso"), or of the department if that is all the narrative names (e.g. "Boyacá"); null if it names neither. Not the city of a clinic they are sent to.
- Include every date the user mentions in fechas_relevantes and every document in anexos_mencionados.
- Use null for an unknown age or special condition.
- In datos_faltantes, ask short questions only for information that is critical to draft the tutela \
(e.g. which EPS, what was denied, whether there is a medical prescription), at most 3. Never ask for the \
person's name, ID number, address or contact data; the document leaves those to fill in. Leave it empty otherwise.
{OUTPUT_LANGUAGE}"""


async def intake(narrative: str, usage: StepUsage) -> CaseFacts:
    """Extract facts with the fast tier; escalate to the draft tier if a core fact is not grounded.

    Ungrounded optional facts (city, special condition) are set to null instead.
    """
    user = f"Narrative:\n{narrative}"
    facts = await structured_fast("intake", CaseFacts, INTAKE_SYSTEM, user, usage, max_tokens=MAX_TOKENS["intake"])
    if usage.escalations:  # the fast tier's output was unusable and the draft tier already answered
        facts, dropped = grounding.drop_ungrounded_optional(facts, narrative)
        usage.notes += dropped
        return facts
    facts, dropped = grounding.drop_ungrounded_optional(facts, narrative)
    usage.notes += dropped
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
    facts, dropped = grounding.drop_ungrounded_optional(facts, narrative)
    usage.notes += dropped
    escalation["remaining_issues"] = grounding.check(facts, narrative)
    usage.escalations.append(escalation)
    return facts


QUESTIONS_SYSTEM = """You help a person in Colombia prepare an acción de tutela against their EPS. Read their \
narrative and decide whether something CRITICAL to draft the tutela is missing. Critical means one of:
- the name of the EPS;
- what exactly was denied or delayed (drug, procedure, appointment, transport...);
- whether a doctor prescribed or ordered it;
- what the EPS did (denied it, never answered, authorized but did not deliver) and roughly when;
- a contradiction on a critical point (e.g. two different ages, a denial dated before the order, a service said \
to be both denied and already provided): ask which version is right.
Ask at most 3 short, plain questions in Spanish (usted), one per missing item, most important first.
Never ask about something the narrative already says, even vaguely or in other words. Never ask for the person's \
name, ID number, address or contact data. If nothing critical is missing, return an empty list."""


async def clarifying_questions(narrative: str, usage: StepUsage, limit: int) -> list[str]:
    """Questions for critical missing information, with the draft tier (the fast tier asks about what is there)."""
    result = await structured(
        "draft", ClarifyingQuestions, QUESTIONS_SYSTEM, f"Narrative:\n{narrative}", usage,
        max_tokens=MAX_TOKENS["questions"],
    )
    return [q.strip() for q in result.preguntas if q.strip()][:limit]


# --- Step 2: legal analysis --------------------------------------------------

ANALYSIS_SYSTEM = f"""You are a Colombian constitutional law analyst. Given the facts of a health-care \
complaint against an EPS, determine:
- the fundamental rights at stake (e.g. salud, vida digna, seguridad social, dignidad humana);
- whether the acción de tutela is admissible (procedencia: subsidiarity, immediacy, standing), with a short reason;
- the urgency (alta / media / baja) given the patient's condition;
Admissibility in health cases: set procede=false only when one of these holds, and say which in razon:
  a) the EPS was never asked: the person has not requested the service from the EPS (no request, no denial, no \
delay by the EPS), and nothing points to a serious risk to life or health that cannot wait for the EPS's answer;
  b) no health service was denied, delayed or interrupted (e.g. rude treatment or long waits when the service was \
provided, billing): the right channel is a complaint (PQRS) to the EPS and then to the Superintendencia Nacional \
de Salud;
  c) the service was already provided, so there is nothing left to order (carencia actual de objeto);
  d) the narrative does not say what was denied or by whom, so there is nothing concrete to ask a judge.
Otherwise procede=true: a denial, an unanswered request, an authorization never delivered or an interrupted \
treatment is enough (a prior derecho de petición is not required), and special protection subjects (minors, older \
adults, persons with disabilities, serious illness) reinforce it. When in doubt between true and false, choose true.
- paso_previo: when procede=false, the concrete step the person should take first, in plain Spanish (e.g. file the \
request with the EPS in writing and keep the filing number); null when procede=true;
- 2 to 4 short Spanish search queries to find relevant Constitutional Court rulings (sentencias de tutela) \
on corteconstitucional.gov.co. Make queries specific to the denied service and the patient's situation.
{OUTPUT_LANGUAGE}"""


async def analysis(narrative: str, facts: CaseFacts, usage: StepUsage) -> LegalAnalysis:
    user = f"Narrative:\n{narrative}\n\nExtracted facts:\n{_dump(facts)}"
    legal = await structured("reasoning", LegalAnalysis, ANALYSIS_SYSTEM, user, usage, max_tokens=MAX_TOKENS["analysis"])
    missing = grounding.missing_core(facts)
    if missing and legal.procedencia_tutela.procede:
        # Deterministic: the reasoning model sometimes accepts a story that names no EPS or no service.
        usage.notes.append(f"not admissible yet: the story does not say {' nor '.join(missing)}")
        legal = legal.model_copy(update={"procedencia_tutela": Procedencia(
            procede=False,
            razon=f"El relato no dice {' ni '.join(missing)}; sin eso no hay una orden concreta que pedirle a un juez.",
            paso_previo=f"Complete su relato indicando {' y '.join(missing)}, y si un médico lo ordenó; luego "
            "vuelva a intentarlo.",
        )})
    return legal


# --- Step 3: research --------------------------------------------------------

RELEVANCE_SYSTEM = f"""You screen Colombian Constitutional Court rulings found by a search engine for a tutela against an EPS. Mark a ruling as relevant (relevante=true) when its excerpt deals with the same kind of problem (the same or a similar denied or delayed service, the same patient situation) OR with the general rules on the right to health that a judge would apply to this case (oportunidad, integralidad, continuidad, services not covered by the PBS, special protection subjects). Mark it as not relevant only when it is clearly about something else (pensions, labor, other areas of law).
For each ruling, summarize in one or two sentences why it matters for this case, based only on the excerpt.
Return one item per ruling, using exactly the sentencia_id given.
{OUTPUT_LANGUAGE}"""


async def _search_all(queries: list[str], usage: StepUsage) -> list[list[SearchResult]]:
    """Run the queries in parallel (each with its own timeout), within the case's search budget.

    Raises SearchUnavailable when every query failed: the case must not be drafted without case law.
    """
    granted = usage.budget.take(len(queries))
    if granted == 0:
        raise SearchBudgetExceeded("Tavily search budget for this case is exhausted")
    queries = queries[:granted]
    depth = get_settings().tavily_research_depth
    outcomes = await asyncio.gather(
        *(usage.search(q, JURISPRUDENCE_DOMAINS, depth) for q in queries), return_exceptions=True
    )
    batches = []
    for query, outcome in zip(queries, outcomes):
        if isinstance(outcome, BaseException):
            logger.warning("tavily query failed query=%r error=%r", query, outcome)
            continue
        batches.append(outcome)
    if not batches:
        first = next((o for o in outcomes if isinstance(o, SearchUnavailable)), None)
        raise first or SearchUnavailable("error", f"all {len(queries)} Tavily searches failed")
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
        raise NoCaseLaw("no_case_law")

    listing = "\n\n".join(
        f"sentencia_id: {p.sentencia_id}\ntitle: {r.title}\nexcerpt: {p.extracto_fuente[:SCREENING_CHARS]}"
        for r, p in candidates.values()
    )
    user = f"Case facts:\n{_dump(facts)}\n\nRulings:\n{listing}"
    review = await structured_fast(
        "research", RelevanceReview, RELEVANCE_SYSTEM, user, usage, max_tokens=MAX_TOKENS["screening"]
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
    if not selected:
        raise NoCaseLaw("no_case_law")
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
- Never write the person's name, ID number, address or contact data; the document template adds them as \n[COMPLETAR: ...] placeholders. If a fact needs one, write a placeholder such as [COMPLETAR: nombre del médico].
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


def build_draft(facts: CaseFacts, sections: TutelaSections) -> TutelaDraft:
    blocks = document.tutela_blocks(facts.eps, facts.ciudad, sections)
    return TutelaDraft(
        **sections.model_dump(), accionada=facts.eps, ciudad=facts.ciudad, texto_completo=document.render_text(blocks)
    )


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
    return build_draft(facts, sections)


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
    # follow-up autos of famous rulings. A lookup by ID needs no advanced depth. A failed search leaves the
    # ruling unverified, so it is removed from the draft.
    outcomes = await asyncio.gather(
        *(usage.search(f"sentencia {sid} relatoria", JURISPRUDENCE_DOMAINS, "basic") for sid in searched),
        return_exceptions=True,
    )
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
    return build_draft(facts, sections)


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
        tutela = build_draft(facts, sections)
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
Keep it short, concrete and in plain language. Never write web addresses, e-mail addresses, phone numbers or \
street addresses.
{OUTPUT_LANGUAGE}"""

CHANNEL_SYSTEM = f"""You read search results from official Colombian websites to tell a person how to file an \
acción de tutela online in a given city. Pick the one result that best describes an official channel for filing \
a tutela (an online filing service, or the e-mail of the oficina judicial / de reparto) that applies to that city \
or to the whole country, and describe it in one or two sentences using only what that result says.
- enlace and correo: copy them only if they appear verbatim in that result's text and are for filing; else null.
- If no result describes a channel for filing a tutela, return fuente = -1.
{OUTPUT_LANGUAGE}"""

GENERAL_CHANNEL = (
    "No se encontró en fuentes oficiales un canal específico para su ciudad. Puede presentar la tutela ante "
    "cualquier juez del lugar donde ocurre la vulneración, en la oficina judicial o de reparto, o por el servicio "
    "“Tutela en línea” de la Rama Judicial. No necesita abogado."
)

SUMMARY_SYSTEM = """You summarize a Colombian acción de tutela (written in Spanish) in English for reviewers who \
do not read Spanish. Be faithful to the draft: no facts, rights, requests or rulings that are not in it. Keep it \
short and plain. For each cited ruling, say in one sentence why the draft cites it.
Write in the third person about "the petitioner" and use they/them; never guess their gender.
All text values in the JSON must be written in English."""

_URL_RE = re.compile(r"(?:https?://|www\.)[^\s)\]>\"'<,;]+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _contact_key(value: str) -> str:
    return re.sub(r"^(https?://)?(www\.)?", "", value.strip().rstrip(".:").lower()).rstrip("/")


def appears_in(value: str, text: str) -> bool:
    """Whether a URL or e-mail address appears in `text` (ignoring scheme, www and case)."""
    key = _contact_key(value)
    haystack = re.sub(r"(https?://)|(www\.)", "", text.lower())
    return bool(key) and key in haystack


def scrub_contacts(text: str, allowed: set[str]) -> str:
    """Remove every URL and e-mail address that does not come from a source that was found."""
    keys = {_contact_key(a) for a in allowed if a}

    def keep(m: re.Match) -> str:
        return m.group() if _contact_key(m.group()) in keys else ""

    text = _URL_RE.sub(keep, _EMAIL_RE.sub(keep, text))
    text = re.sub(r"\(\s*\)|\[\s*\]", "", text)
    return re.sub(r"[ \t]{2,}", " ", re.sub(r"[ \t]+([.,;:])", r"\1", text)).strip()


_REGIONAL_RE = re.compile(r"seccional|distrito judicial|tribunal superior", re.IGNORECASE)


def channel_applies(ciudad: str, source: SearchResult) -> bool:
    """A source applies if it names the city (or department) or is not a regional office's page."""
    text = grounding.fold(f"{source.url} {source.title} {source.content}").replace("-", " ")
    if grounding.fold(ciudad) in text:
        return True
    return not _REGIONAL_RE.search(f"{source.url.replace('-', ' ')} {source.title}")


async def filing_channel(ciudad: str, usage: StepUsage) -> FilingChannel:
    """The official channel to file a tutela online in `ciudad`, from .gov.co sources found with Tavily."""
    consulted = datetime.now(UTC).date().isoformat()
    general = FilingChannel(ciudad=ciudad, encontrado=False, descripcion=GENERAL_CHANNEL, consultado=consulted)
    queries = [
        (f"recepción de tutelas en línea {ciudad} Rama Judicial", ["ramajudicial.gov.co"]),
        (f"radicar acción de tutela en {ciudad}", OFFICIAL_DOMAINS),
    ][: usage.budget.take(2)]
    if not queries:
        return general
    outcomes = await asyncio.gather(*(usage.search(q, d, "basic") for q, d in queries), return_exceptions=True)
    results: list[SearchResult] = []
    for (query, _), outcome in zip(queries, outcomes):
        if isinstance(outcome, BaseException):
            logger.warning("channel search failed query=%r error=%r", query, outcome)
            continue
        seen = {r.url for r in results}
        results += [r for r in outcome if r.url not in seen and is_official(r.url)]
    results = [r for r in results if channel_applies(ciudad, r)][:8]
    if not results:
        return general

    listing = "\n\n".join(
        f"[{i}] url: {r.url}\ntitle: {r.title}\ntext: {r.content[:SCREENING_CHARS]}" for i, r in enumerate(results)
    )
    extraction = await structured_fast(
        "package", ChannelExtraction, CHANNEL_SYSTEM, f"City: {ciudad}\n\nResults:\n{listing}", usage,
        max_tokens=MAX_TOKENS["channel"],
    )
    if not 0 <= extraction.fuente < len(results):
        return general
    source = results[extraction.fuente]
    source_text = f"{source.url} {source.title} {source.content[:SCREENING_CHARS]}"
    if not channel_applies(ciudad, source):
        # A regional office's page (e.g. another department's Consejo Seccional) does not apply to this city.
        usage.notes.append(f"filing channel source is a regional page that does not name {ciudad}: {source.title!r}")
        return general
    # Never show a link or e-mail address that is not in the source the model was shown.
    enlace = extraction.enlace if extraction.enlace and appears_in(extraction.enlace, source_text) else None
    correo = extraction.correo if extraction.correo and appears_in(extraction.correo, source_text) else None
    descripcion = scrub_contacts(extraction.descripcion, {enlace or "", correo or "", source.url})
    if descripcion != extraction.descripcion.strip():
        # The model wrote a link or address the source does not contain; removing it leaves a broken
        # sentence, so point to the source instead.
        descripcion = (
            f"La fuente oficial citada publica los medios autorizados para presentar tutelas en línea en {ciudad}. "
            "Consulte allí el enlace o el correo vigente."
        )
    return FilingChannel(
        ciudad=ciudad,
        encontrado=True,
        descripcion=descripcion,
        enlace=enlace,
        correo=correo,
        fuente_url=source.url,
        fuente_titulo=source.title or None,
        consultado=consulted,
    )


def summary_issues(summary: ReviewerSummary, tutela: TutelaDraft) -> list[str]:
    """Deterministic consistency check of the English summary against the Spanish draft."""
    issues = []
    if len(summary.rights_invoked) > len(tutela.derechos_vulnerados) + 1:
        issues.append(f"{len(summary.rights_invoked)} rights in the summary, {len(tutela.derechos_vulnerados)} in the draft")
    if any(len(r.split()) > 10 for r in summary.rights_invoked):
        issues.append("a right in the summary is a sentence, not a right")
    if not summary.requests or len(summary.requests) > len(tutela.pretensiones) + 1:
        issues.append(f"{len(summary.requests)} requests in the summary, {len(tutela.pretensiones)} in the draft")
    eps_words = [w for w in grounding.fold(tutela.accionada).split() if w != "eps"]
    if eps_words and not all(w in grounding.fold(summary.facts) for w in eps_words):
        issues.append(f"the facts do not name the EPS {tutela.accionada!r}")
    return issues


async def reviewer_summary(tutela: TutelaDraft, report: VerificationReport, usage: StepUsage) -> ReviewerSummary:
    """English summary of the final draft; only verified rulings are kept."""
    verified = [c.sentencia_id for c in report.citas_verificadas]
    sections = json.dumps(tutela.model_dump(include=set(_TEXT_FIELDS)), ensure_ascii=False, indent=2)
    user = f"Draft sections (Spanish):\n{sections}\n\nCited rulings: {', '.join(verified) or 'none'}"
    # Own usage: the filing-channel search runs in parallel on the step's usage and may escalate too.
    own = StepUsage(budget=usage.budget, searcher=usage.searcher, emit=usage.emit)
    try:
        summary = await _checked_summary(tutela, user, own)
    finally:
        usage.calls += own.calls
        usage.escalations += own.escalations
    rulings: list[ReviewerRuling] = []
    for r in summary.rulings:
        ids = citations.find_citations(r.ruling_id)
        if ids and ids[0] in verified and ids[0] not in {x.ruling_id for x in rulings}:
            rulings.append(r.model_copy(update={"ruling_id": ids[0]}))
    return summary.model_copy(update={"rulings": rulings})


async def _checked_summary(tutela: TutelaDraft, user: str, usage: StepUsage) -> ReviewerSummary:
    """The summary on the fast tier, escalated to the draft tier if unusable or inconsistent with the draft."""
    summary = await structured_fast(
        "package", ReviewerSummary, SUMMARY_SYSTEM, user, usage, max_tokens=MAX_TOKENS["summary"]
    )
    issues = summary_issues(summary, tutela) if not usage.escalations else []
    if issues:
        escalation = {
            "step": "package", "from_tier": "fast", "from_model": get_settings().model_for_tier("fast"),
            "to_tier": "draft", "to_model": get_settings().model_for_tier("draft"), "reasons": issues,
        }
        logger.warning("reviewer summary inconsistent with the draft, escalating: %s", issues)
        usage.emit("escalated", escalation)
        summary = await structured(
            "draft", ReviewerSummary, SUMMARY_SYSTEM, user, usage, max_tokens=MAX_TOKENS["summary"]
        )
        escalation["remaining_issues"] = summary_issues(summary, tutela)
        usage.escalations.append(escalation)
    return summary


NEXT_STEPS_SYSTEM = f"""A person in Colombia asked for help to file an acción de tutela against their EPS, but the \
legal analysis found that a tutela is not the right tool yet. Explain it to them kindly and in plain language:
- por_que_no_ahora: why a tutela would most likely be rejected now, based on the analysis (two or three sentences);
- pasos: 2 to 5 concrete steps to take first, in order (e.g. file a written request (derecho de petición) with \
the EPS and keep the filing number; the EPS must answer within the legal term; a complaint (PQRS) to the EPS and \
then to the Superintendencia Nacional de Salud; what documents to keep);
- cuando_procede: when a tutela would become the right tool (e.g. if the EPS denies the service, does not answer \
in time, or the health situation becomes urgent).
Use only the facts given. Never write web addresses, e-mail addresses, phone numbers or street addresses.
{OUTPUT_LANGUAGE}"""


async def next_steps(facts: CaseFacts, legal: LegalAnalysis, usage: StepUsage) -> FinalPackage:
    """Delivery when the tutela is not admissible yet: why, and what to do first (draft tier). No document."""
    user = (
        f"Case facts:\n{_dump(facts)}\n\nAnalysis: procede=false\nReason: {legal.procedencia_tutela.razon}\n"
        f"Step to take first: {legal.procedencia_tutela.paso_previo or '(not given)'}"
    )
    steps = await structured("draft", NextSteps, NEXT_STEPS_SYSTEM, user, usage, max_tokens=MAX_TOKENS["package"])
    steps = NextSteps(
        por_que_no_ahora=scrub_contacts(steps.por_que_no_ahora, set()),
        pasos=[scrub_contacts(p, set()) for p in steps.pasos if p.strip()],
        cuando_procede=scrub_contacts(steps.cuando_procede, set()),
    )
    return FinalPackage(
        tutela_recomendada=False, orientacion=steps, draft=None, verification=None,
        checklist_anexos=[], donde_radicar="", que_hacer_si_no_cumplen="", aviso=DISCLAIMER,
    )


async def _nothing() -> None:
    return None


async def package(
    facts: CaseFacts,
    legal: LegalAnalysis,
    tutela: TutelaDraft,
    report: VerificationReport,
    usage: StepUsage,
    english_summary: bool = False,
) -> FinalPackage:
    user = (
        f"Case facts:\n{_dump(facts)}\n\nUrgency: {legal.urgencia}\n\n"
        f"Draft annexes:\n{_numbered(tutela.pruebas_y_anexos)}"
    )
    # Filing guidance is legal orientation the person reads as is: draft tier (the fast tier garbled it).
    guidance_usage = StepUsage(budget=usage.budget, searcher=usage.searcher)
    guidance, channel, summary = await asyncio.gather(
        structured("draft", PackageGuidance, PACKAGE_SYSTEM, user, guidance_usage, max_tokens=MAX_TOKENS["package"]),
        filing_channel(facts.ciudad, usage) if facts.ciudad else _nothing(),
        reviewer_summary(tutela, report, usage) if english_summary else _nothing(),
    )
    usage.calls += guidance_usage.calls
    usage.resolved_by = guidance_usage.calls[-1].model
    allowed = {x for x in (channel.enlace, channel.correo, channel.fuente_url) if x} if channel else set()
    guidance = guidance.model_copy(update={
        "donde_radicar": scrub_contacts(guidance.donde_radicar, allowed),
        "que_hacer_si_no_cumplen": scrub_contacts(guidance.que_hacer_si_no_cumplen, allowed),
    })
    return FinalPackage(
        **guidance.model_dump(), draft=tutela, verification=report, canal_radicacion=channel,
        resumen_revisores=summary, aviso=DISCLAIMER,
    )


# --- Orchestration -----------------------------------------------------------

STEPS = ("intake", "analysis", "research", "draft", "verify", "package")
# Skipped when the analysis finds that the tutela is not admissible yet.
SKIPPABLE = ("research", "draft", "verify")


def with_answers(narrative: str, answers: list[tuple[str, str]]) -> str:
    """The narrative plus the person's answers to the clarifying questions (skipped ones are left out)."""
    answered = [(q.strip(), a.strip()) for q, a in answers if a.strip()]
    if not answered:
        return narrative
    lines = "\n".join(f"- Pregunta: {q}\n  Respuesta: {a}" for q, a in answered)
    return f"{narrative}\n\nRespuestas a preguntas aclaratorias:\n{lines}"


async def run_case(
    narrative: str, english_summary: bool = False, searcher: Searcher | None = None
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Run all steps, yielding (event, payload) tuples for each step and the final result.

    english_summary adds an English summary of the final draft for reviewers (fast tier). searcher sets
    the cache and the daily credit cap for Tavily (default: cache on, no cap).

    If research finds no case law (search unavailable, or nothing relevant), the case ends with
    case_unavailable instead of drafting a tutela without verified rulings.
    """
    state: dict[str, Any] = {}
    totals = {key: 0 for key in _SUMMED} | {"escalations": 0}
    searcher = searcher or Searcher()

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
        if not admissible():
            state["package"] = await next_steps(state["facts"], state["legal"], u)
        else:
            state["package"] = await package(
                state["facts"], state["legal"], state["draft"], state["report"], u, english_summary
            )
        return state["package"]

    def admissible() -> bool:
        return state["legal"].procedencia_tutela.procede

    runners: dict[str, Callable[[StepUsage], Awaitable[BaseModel]]] = {
        "intake": step_intake,
        "analysis": step_analysis,
        "research": step_research,
        "draft": step_draft,
        "verify": step_verify,
        "package": step_package,
    }

    budget = SearchBudget(get_settings().max_tavily_searches_per_case)
    stopped: NoCaseLaw | SearchUnavailable | None = None
    for index, name in enumerate(STEPS, 1):
        if stopped is not None:
            yield "step_skipped", {"step": name, "index": index, "reason": "no_case_law"}
            continue
        if name in SKIPPABLE and not admissible():
            # A tutela that would be rejected is not drafted; delivery explains what to do first.
            yield "step_skipped", {"step": name, "index": index, "reason": "La tutela no procede todavía."}
            continue
        yield "step_started", {"step": name, "index": index, "total": len(STEPS)}
        events: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        usage = StepUsage(budget=budget, searcher=searcher, emit=lambda event, data: events.put_nowait((event, data)))
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
        except (NoCaseLaw, SearchUnavailable) as exc:
            # No rulings to cite: stop before drafting, without failing the case.
            logger.warning("no case law for this case: %s", exc)
            stopped = exc
            metrics = usage.metrics() | {"latency_s": round(time.perf_counter() - start, 2)}
            _accumulate(totals, metrics)
            reason = exc.reason if isinstance(exc, NoCaseLaw) else "search_unavailable"
            yield "step_unavailable", {
                "step": name, "index": index, "reason": reason, "message": UNAVAILABLE_MESSAGES[reason],
                "metrics": metrics,
            }
            continue
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

    if stopped is not None:
        reason = stopped.reason if isinstance(stopped, NoCaseLaw) else "search_unavailable"
        yield "case_unavailable", {"reason": reason, "message": UNAVAILABLE_MESSAGES[reason], "totals": _round(totals)}
        return
    yield "case_completed", {"package": state["package"].model_dump(), "totals": _round(totals)}


_SUMMED = (
    "input_tokens", "output_tokens", "cost_usd", "llm_cost_usd", "search_cost_usd", "cost_all_reasoning_usd",
    "latency_s", "tavily_searches", "tavily_live", "tavily_cached", "tavily_credits",
)


def _accumulate(totals: dict[str, Any], metrics: dict[str, Any]) -> None:
    for key in _SUMMED:
        totals[key] += metrics[key]
    totals["escalations"] += len(metrics["escalations"])


def _round(totals: dict[str, Any]) -> dict[str, Any]:
    return totals | {
        key: round(totals[key], 6) for key in ("cost_usd", "llm_cost_usd", "search_cost_usd", "cost_all_reasoning_usd")
    } | {"latency_s": round(totals["latency_s"], 2)}


def _error_message(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}"
    return message if len(message) <= 500 else message[:497] + "..."
