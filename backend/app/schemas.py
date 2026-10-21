"""Pydantic models for each pipeline step.

Field names are in Spanish because they mirror the sections of the legal
document the user receives; descriptions guide the LLM's structured output.
"""

from typing import Literal

from pydantic import BaseModel, Field

# --- Step 1: intake ---------------------------------------------------------


class FechaRelevante(BaseModel):
    fecha: str = Field(description="Date as stated by the user (ISO if possible, otherwise free text)")
    evento: str = Field(description="What happened on that date, in Spanish")


class SituacionPaciente(BaseModel):
    edad: int | None = Field(description="Patient age in years, null if unknown")
    condicion_especial: str | None = Field(
        description="Special condition (older adult, disability, pregnancy, minor, chronic illness...), null if none"
    )


class CaseFacts(BaseModel):
    eps: str = Field(description="Name of the EPS (health insurer)")
    servicio_negado: str = Field(description="Health service, drug, procedure or support that was denied or delayed")
    diagnostico: str = Field(description="Diagnosis, as described by the user")
    fechas_relevantes: list[FechaRelevante]
    situacion_del_paciente: SituacionPaciente
    anexos_mencionados: list[str] = Field(description="Documents the user mentions having (prescriptions, denials...)")
    datos_faltantes: list[str] = Field(
        description="Questions in Spanish for critical missing information; empty if nothing critical is missing"
    )


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
    texto_completo: str


# --- Step 5: verification ---------------------------------------------------


class CitationCheck(BaseModel):
    sentencia_id: str
    estado: Literal["verificada", "no_verificada"]
    url: str | None = None
    detalle: str


class VerificationReport(BaseModel):
    citas_encontradas: list[str]
    citas_verificadas: list[CitationCheck]
    citas_no_verificadas: list[CitationCheck]
    observaciones: list[str]


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


class FinalPackage(PackageGuidance):
    draft: TutelaDraft
    verification: VerificationReport
    aviso: str
