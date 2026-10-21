"""Pydantic models for each pipeline step.

Field names are in Spanish because they mirror the sections of the legal
document the user receives; descriptions guide the LLM's structured output.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field, field_validator

# --- Step 1: intake ---------------------------------------------------------

_NULL_STRINGS = {
    "", "null", "none", "n/a", "na", "nulo", "nula", "desconocido", "desconocida", "no especificado", "ninguno", "ninguna",
}


def _null_string(value: object) -> object:
    """Small models sometimes write the string "null" instead of null."""
    return None if isinstance(value, str) and value.strip().lower() in _NULL_STRINGS else value


OptionalText = Annotated[str | None, BeforeValidator(_null_string)]


class FechaRelevante(BaseModel):
    fecha: str = Field(description="Date as stated by the user (ISO if possible, otherwise free text)")
    evento: str = Field(description="What happened on that date, in Spanish")


class SituacionPaciente(BaseModel):
    edad: int | None = Field(description="Patient age in years, null if unknown")
    condicion_especial: OptionalText = Field(
        description="Special condition (older adult, disability, pregnancy, minor, chronic illness...), null if none"
    )


class CaseFacts(BaseModel):
    eps: str = Field(description="Name of the EPS (health insurer)")
    servicio_negado: str = Field(description="Health service, drug, procedure or support that was denied or delayed")
    diagnostico: str = Field(description="Diagnosis, as described by the user")
    ciudad: OptionalText = Field(
        description="Name of the municipality (or department) where the person lives, if the narrative names it"
    )
    fechas_relevantes: list[FechaRelevante]
    situacion_del_paciente: SituacionPaciente
    anexos_mencionados: list[str] = Field(description="Documents the user mentions having (prescriptions, denials...)")
    datos_faltantes: list[str] = Field(
        description="Questions in Spanish for critical missing information; empty if nothing critical is missing"
    )

    @field_validator("fechas_relevantes")
    @classmethod
    def _drop_empty_dates(cls, value: list[FechaRelevante]) -> list[FechaRelevante]:
        return [f for f in value if f.fecha.strip()]


class ClarifyingQuestions(BaseModel):
    preguntas: list[str] = Field(description="0 to 3 short questions in Spanish for critical missing information")


# --- Step 2: legal analysis -------------------------------------------------


class Procedencia(BaseModel):
    procede: bool
    razon: str = Field(description="Why the tutela is or is not admissible, in Spanish")


class LegalAnalysis(BaseModel):
    derechos_vulnerados: list[str] = Field(description="Fundamental rights at stake, in Spanish")
    procedencia_tutela: Procedencia
    urgencia: Literal["alta", "media", "baja"]
    queries_de_busqueda: list[str] = Field(
        description="2 to 4 Spanish search queries to find Constitutional Court rulings", min_length=2, max_length=4
    )


# --- Step 3: research -------------------------------------------------------


class Precedent(BaseModel):
    sentencia_id: str = Field(description="Normalized ruling ID, e.g. T-760/08")
    url: str
    resumen_relevancia: str = Field(description="Why this ruling is relevant to the case, in Spanish")
    extracto_fuente: str = Field(description="Excerpt returned by the search engine")
    origen: Literal["busqueda", "busqueda_dirigida"] = "busqueda"
    charset: str | None = Field(default=None, description="Charset of the ruling page, if it was downloaded")
    acentos_restaurados: int = Field(default=0, description="Words of the excerpt repaired from the page text")
    citas_mencionadas: list[str] = Field(default_factory=list, description="Rulings cited inside the full text")


class Precedents(BaseModel):
    precedentes: list[Precedent]


class RelevanceItem(BaseModel):
    sentencia_id: str
    relevante: bool
    resumen_relevancia: str = Field(description="One or two sentences in Spanish")


class RelevanceReview(BaseModel):
    items: list[RelevanceItem]


# --- Step 4: draft ----------------------------------------------------------


class TutelaSections(BaseModel):
    """What the LLM writes; texto_completo is assembled deterministically."""

    hechos: list[str] = Field(description="Numbered facts in Spanish, first person of the plaintiff")
    derechos_vulnerados: list[str]
    fundamentos_juridicos: str = Field(description="Legal grounds in Spanish, citing only the provided rulings")
    pretensiones: list[str] = Field(description="Specific orders requested from the judge, in Spanish")
    pruebas_y_anexos: list[str]
    juramento: str = Field(description="Sworn statement that no other tutela was filed for the same facts")


class TutelaDraft(TutelaSections):
    accionada: str = Field(description="The EPS the tutela is filed against")
    ciudad: str | None = None
    texto_completo: str


# --- Step 5: verification ---------------------------------------------------


class CitationCheck(BaseModel):
    sentencia_id: str
    estado: Literal["verificada", "no_verificada"]
    url: str | None = None
    detalle: str


class CitationOutcome(BaseModel):
    """What the verification loop did with a citation that was not verified at first."""

    sentencia_id: str
    resultado: Literal["verificada_busqueda_dirigida", "retirada_reescritura", "retirada_automatica"]
    iteracion: int
    url: str | None = None
    detalle: str


class VerificationReport(BaseModel):
    citas_encontradas: list[str]
    citas_verificadas: list[CitationCheck]
    citas_no_verificadas: list[CitationCheck] = Field(description="Unverified citations left in the final text")
    citas_no_verificadas_iniciales: list[str] = Field(default_factory=list)
    ciclo: list[CitationOutcome] = Field(default_factory=list)
    iteraciones: int = 0
    observaciones: list[str]


class ParagraphRewrite(BaseModel):
    parrafos: list[str] = Field(description="The rewritten paragraphs, same count and order as given, in Spanish")


class CoherenceReview(BaseModel):
    coherente: bool = Field(description="Whether every request is supported by the stated facts")
    observaciones: list[str] = Field(description="Concrete issues found, in Spanish; empty if none")


# --- Step 6: package --------------------------------------------------------


class PackageGuidance(BaseModel):
    checklist_anexos: list[str] = Field(description="Documents to attach, in Spanish")
    donde_radicar: str = Field(description="Where and how to file the tutela in Colombia, in Spanish")
    que_hacer_si_no_cumplen: str = Field(
        description="What to do if the EPS does not comply with the ruling (incidente de desacato), in Spanish"
    )


class ChannelExtraction(BaseModel):
    """What the fast tier reads from official search results about filing a tutela online."""

    fuente: int = Field(description="Index of the result that describes the channel, or -1 if none does")
    descripcion: str = Field(description="How to file a tutela online there, in Spanish, based only on that result")
    enlace: str | None = Field(description="Filing URL exactly as written in that result, or null")
    correo: str | None = Field(description="Filing e-mail address exactly as written in that result, or null")


class FilingChannel(BaseModel):
    """Official channel to file a tutela online in the person's city, found with Tavily."""

    ciudad: str
    encontrado: bool
    descripcion: str
    enlace: str | None = None
    correo: str | None = None
    fuente_url: str | None = None
    fuente_titulo: str | None = None
    consultado: str = Field(description="Date of the search (ISO, UTC)")


class ReviewerRuling(BaseModel):
    ruling_id: str
    why: str = Field(description="Why it supports the case, one sentence in English")


class ReviewerSummary(BaseModel):
    """Short English summary of the final draft for reviewers who do not read Spanish."""

    facts: str = Field(description="The facts in 2 to 4 sentences, in English")
    rights_invoked: list[str] = Field(description="Fundamental rights invoked, in English")
    requests: list[str] = Field(description="What the judge is asked to order, in English")
    rulings: list[ReviewerRuling] = Field(description="Each cited ruling and why it is cited")


class FinalPackage(PackageGuidance):
    draft: TutelaDraft
    verification: VerificationReport
    canal_radicacion: FilingChannel | None = None
    resumen_revisores: ReviewerSummary | None = None
    aviso: str
