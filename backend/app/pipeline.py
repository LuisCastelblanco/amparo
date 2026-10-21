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

from app import citations
from app.llm import ChatResult, Tier, chat
from app.schemas import (
    CaseFacts,
    CitationCheck,
    CoherenceReview,
    FinalPackage,
    LegalAnalysis,
    PackageGuidance,
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
UNVERIFIED_MARKER = "[CITA NO VERIFICADA]"
DISCLAIMER = (
    "Amparo es una herramienta de ayuda para la redacción y no constituye asesoría legal. "
    "Revise el documento, complete los datos entre corchetes y, si es posible, consúltelo con un "
    "abogado, un consultorio jurídico universitario o la Defensoría del Pueblo antes de radicarlo."
)
OUTPUT_LANGUAGE = "All user-facing text values in the JSON must be written in Spanish (Colombia)."


# --- Usage tracking ----------------------------------------------------------


@dataclass
class StepUsage:
    calls: list[ChatResult] = field(default_factory=list)
    tavily_searches: int = 0

    def metrics(self) -> dict[str, Any]:
        costs = [c.cost_usd for c in self.calls]
        return {
            "models": list(dict.fromkeys(c.model for c in self.calls)),
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
    max_tokens: int = 16384,
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
- Include every date the user mentions in fechas_relevantes and every document in anexos_mencionados.
- Use null for an unknown age or special condition.
- In datos_faltantes, ask short questions only for information that is critical to draft the tutela \
(e.g. which EPS, what was denied, whether there is a medical prescription). Leave it empty otherwise.
{OUTPUT_LANGUAGE}"""


async def intake(narrative: str, usage: StepUsage) -> CaseFacts:
    return await structured("fast", CaseFacts, INTAKE_SYSTEM, f"Narrative:\n{narrative}", usage, max_tokens=8192)


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
    return await structured("reasoning", LegalAnalysis, ANALYSIS_SYSTEM, user, usage)


# --- Step 3: research --------------------------------------------------------

RELEVANCE_SYSTEM = f"""You screen Colombian Constitutional Court rulings found by a search engine for a tutela against an EPS. Mark a ruling as relevant (relevante=true) when its excerpt deals with the same kind of problem (the same or a similar denied or delayed service, the same patient situation) OR with the general rules on the right to health that a judge would apply to this case (oportunidad, integralidad, continuidad, services not covered by the PBS, special protection subjects). Mark it as not relevant only when it is clearly about something else (pensions, labor, other areas of law).
For each ruling, summarize in one or two sentences why it matters for this case, based only on the excerpt.
Return one item per ruling, using exactly the sentencia_id given.
{OUTPUT_LANGUAGE}"""


async def _search_all(queries: list[str], usage: StepUsage) -> list[list[SearchResult]]:
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


async def research(facts: CaseFacts, legal: LegalAnalysis, usage: StepUsage) -> Precedents:
    batches = await _search_all(legal.queries_de_busqueda, usage)

    candidates: dict[str, tuple[SearchResult, Precedent]] = {}
    seen_urls: set[str] = set()
    for result in _round_robin(batches):
        url = result.url.split("#")[0]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        sentencia_id = citations.id_from_source(result.title, url)
        if sentencia_id is None or sentencia_id in candidates:
            continue
        candidates[sentencia_id] = (result, Precedent(
            sentencia_id=sentencia_id,
            url=url,
            resumen_relevancia="",
            extracto_fuente=result.content[:EXCERPT_CHARS],
        ))
        if len(candidates) == MAX_CANDIDATES:
            break
    if not candidates:
        return Precedents(precedentes=[])

    listing = "\n\n".join(
        f"sentencia_id: {p.sentencia_id}\ntitle: {r.title}\nexcerpt: {p.extracto_fuente[:SCREENING_CHARS]}"
        for r, p in candidates.values()
    )
    user = f"Case facts:\n{_dump(facts)}\n\nRulings:\n{listing}"
    review = await structured("fast", RelevanceReview, RELEVANCE_SYSTEM, user, usage, max_tokens=8192)

    reviewed = {item.sentencia_id: item for item in review.items}
    selected = []
    for _, precedent in candidates.values():
        item = reviewed.get(precedent.sentencia_id)
        if item is not None and not item.relevante:
            continue
        if item is not None:
            precedent.resumen_relevancia = item.resumen_relevancia
        selected.append(precedent)
    return Precedents(precedentes=selected[:MAX_PRECEDENTS])


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
    sections = await structured("draft", TutelaSections, DRAFT_SYSTEM, user, usage)
    if precedents.precedentes and not citations.find_citations(sections.fundamentos_juridicos):
        logger.warning("draft cited no allowed ruling; retrying once")
        reminder = (
            "\n\nIMPORTANT: a previous attempt cited no ruling. fundamentos_juridicos must cite the applicable "
            'ALLOWED RULINGS as "Sentencia T-123 de 2024".'
        )
        sections = await structured("draft", TutelaSections, DRAFT_SYSTEM, user + reminder, usage)
    return TutelaDraft(**sections.model_dump(), texto_completo=assemble_tutela(facts, sections))


# --- Step 5: verification ----------------------------------------------------

COHERENCE_SYSTEM = f"""You review a draft acción de tutela for internal coherence. Check that:
- every pretensión is supported by the hechos;
- the hechos match the extracted case facts (no invented facts, dates or documents);
- the derechos vulnerados follow from the hechos.
Do not evaluate citations of rulings; they are checked separately. List concrete issues only.
{OUTPUT_LANGUAGE}"""


async def verify(
    facts: CaseFacts, tutela: TutelaDraft, precedents: Precedents, usage: StepUsage
) -> tuple[TutelaDraft, VerificationReport]:
    by_id = {p.sentencia_id: p for p in precedents.precedentes}
    mentioned_in: dict[str, Precedent] = {}
    for p in precedents.precedentes:
        for cited in citations.find_citations(p.extracto_fuente):
            mentioned_in.setdefault(cited, p)

    found = citations.find_citations(tutela.texto_completo)
    verified: list[CitationCheck] = []
    unverified: list[CitationCheck] = []
    for sid in found:
        if sid in by_id:
            verified.append(CitationCheck(
                sentencia_id=sid, estado="verificada", url=by_id[sid].url,
                detalle="Coincide con una sentencia encontrada en la búsqueda.",
            ))
        elif sid in mentioned_in:
            source = mentioned_in[sid]
            verified.append(CitationCheck(
                sentencia_id=sid, estado="verificada", url=source.url,
                detalle=f"Verificación indirecta: aparece citada dentro de la sentencia {source.sentencia_id}.",
            ))
        else:
            unverified.append(CitationCheck(
                sentencia_id=sid, estado="no_verificada",
                detalle="No aparece en las fuentes consultadas. Verifíquela antes de radicar o retírela.",
            ))

    observaciones: list[str] = []
    if unverified:
        flagged = {c.sentencia_id for c in unverified}
        tutela = tutela.model_copy(update={
            "texto_completo": citations.annotate(tutela.texto_completo, flagged, UNVERIFIED_MARKER),
            "fundamentos_juridicos": citations.annotate(tutela.fundamentos_juridicos, flagged, UNVERIFIED_MARKER),
        })
        observaciones.append(
            f"{len(unverified)} cita(s) no verificada(s) quedaron marcadas en el texto con {UNVERIFIED_MARKER}."
        )
    if not found:
        observaciones.append("El borrador no cita sentencias de la Corte Constitucional.")

    sections = tutela.model_dump(exclude={"texto_completo"})
    user = f"Case facts:\n{_dump(facts)}\n\nDraft sections:\n{json.dumps(sections, ensure_ascii=False, indent=2)}"
    review = await structured("reasoning", CoherenceReview, COHERENCE_SYSTEM, user, usage)
    observaciones += review.observaciones

    report = VerificationReport(
        citas_encontradas=found,
        citas_verificadas=verified,
        citas_no_verificadas=unverified,
        observaciones=observaciones,
    )
    return tutela, report


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
    guidance = await structured("fast", PackageGuidance, PACKAGE_SYSTEM, user, usage, max_tokens=8192)
    return FinalPackage(**guidance.model_dump(), draft=tutela, verification=report, aviso=DISCLAIMER)


# --- Orchestration -----------------------------------------------------------

STEPS = ("intake", "analysis", "research", "draft", "verify", "package")


async def run_case(narrative: str) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Run all steps, yielding (event, payload) tuples for each step and the final result."""
    state: dict[str, Any] = {}
    totals = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "latency_s": 0.0, "tavily_searches": 0}

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
        state["draft"], state["report"] = await verify(state["facts"], state["draft"], state["precedents"], u)
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

    for index, name in enumerate(STEPS, 1):
        yield "step_started", {"step": name, "index": index, "total": len(STEPS)}
        usage = StepUsage()
        start = time.perf_counter()
        try:
            result = await runners[name](usage)
        except Exception as exc:  # noqa: BLE001 - reported to the client as step_failed
            logger.exception("step %s failed", name)
            metrics = usage.metrics() | {"latency_s": round(time.perf_counter() - start, 2)}
            yield "step_failed", {"step": name, "index": index, "error": _error_message(exc), "metrics": metrics}
            yield "case_failed", {"failed_step": name, "totals": _round(totals)}
            return
        metrics = usage.metrics() | {"latency_s": round(time.perf_counter() - start, 2)}
        for key in ("input_tokens", "output_tokens", "cost_usd", "latency_s", "tavily_searches"):
            totals[key] += metrics[key]
        yield "step_completed", {"step": name, "index": index, "result": result.model_dump(), "metrics": metrics}

    yield "case_completed", {"package": state["package"].model_dump(), "totals": _round(totals)}


def _round(totals: dict[str, Any]) -> dict[str, Any]:
    return totals | {"cost_usd": round(totals["cost_usd"], 6), "latency_s": round(totals["latency_s"], 2)}


def _error_message(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}"
    return message if len(message) <= 500 else message[:497] + "..."
