export const STEPS = ["intake", "analysis", "research", "draft", "verify", "package"] as const;
export type StepName = (typeof STEPS)[number];

export const STEP_LABELS: Record<StepName, { title: string; description: string }> = {
  intake: { title: "Intake", description: "Extract the facts of the case" },
  analysis: { title: "Legal analysis", description: "Rights at stake, admissibility, urgency" },
  research: { title: "Case-law search", description: "Constitutional Court rulings via Tavily" },
  draft: { title: "Drafting", description: "Write the tutela in Spanish" },
  verify: { title: "Citation check", description: "Verify every cited ruling and coherence" },
  package: { title: "Delivery", description: "Annex checklist and filing guidance" },
};

export interface Escalation {
  step: StepName;
  from_tier: string;
  from_model: string;
  to_tier: string;
  to_model: string;
  reasons: string[];
  remaining_issues?: string[];
}

export interface VerifyIteration {
  step: "verify";
  iteration: number;
  phase: "searching" | "done";
  unverified?: string[];
  found?: string[];
  removed?: string[];
  still_unverified?: string[];
}

export interface StepMetrics {
  models: string[];
  final_model: string | null;
  escalations: Escalation[];
  llm_calls: number;
  input_tokens: number;
  output_tokens: number;
  llm_latency_s: number;
  latency_s: number;
  cost_usd: number;
  cost_complete: boolean;
  /** Tiers used by the step's LLM calls ("fast", "draft", "reasoning"). */
  tiers: string[];
  /** The same calls and token counts priced at the reasoning tier (Ultra). */
  cost_all_reasoning_usd: number;
  /** Deterministic corrections made without an LLM call. */
  notes: string[];
  tavily_searches: number;
}

export interface CitationCheck {
  sentencia_id: string;
  estado: "verificada" | "no_verificada";
  url: string | null;
  detalle: string;
}

export interface CitationOutcome {
  sentencia_id: string;
  resultado: "verificada_busqueda_dirigida" | "retirada_reescritura" | "retirada_automatica";
  iteracion: number;
  url: string | null;
  detalle: string;
}

export interface VerificationReport {
  citas_encontradas: string[];
  citas_verificadas: CitationCheck[];
  citas_no_verificadas: CitationCheck[];
  citas_no_verificadas_iniciales: string[];
  ciclo: CitationOutcome[];
  iteraciones: number;
  observaciones: string[];
}

export interface TutelaDraft {
  hechos: string[];
  derechos_vulnerados: string[];
  fundamentos_juridicos: string;
  pretensiones: string[];
  pruebas_y_anexos: string[];
  juramento: string;
  accionada: string;
  ciudad: string | null;
  texto_completo: string;
}

export interface FilingChannel {
  ciudad: string;
  encontrado: boolean;
  descripcion: string;
  enlace: string | null;
  correo: string | null;
  fuente_url: string | null;
  fuente_titulo: string | null;
  consultado: string;
}

export interface ReviewerSummary {
  facts: string;
  rights_invoked: string[];
  requests: string[];
  rulings: { ruling_id: string; why: string }[];
}

/** What to do first when a tutela is not the right tool yet. */
export interface NextSteps {
  por_que_no_ahora: string;
  pasos: string[];
  cuando_procede: string;
}

export interface FinalPackage {
  /** False when the analysis found the tutela is not admissible yet: no draft, only next steps. */
  tutela_recomendada: boolean;
  draft: TutelaDraft | null;
  verification: VerificationReport | null;
  orientacion: NextSteps | null;
  checklist_anexos: string[];
  donde_radicar: string;
  que_hacer_si_no_cumplen: string;
  canal_radicacion: FilingChannel | null;
  resumen_revisores: ReviewerSummary | null;
  aviso: string;
}

export interface Totals {
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  latency_s: number;
  tavily_searches: number;
  escalations?: number;
  cost_all_reasoning_usd?: number;
}

export type StepStatus = "pending" | "running" | "done" | "failed" | "skipped";

export interface StepState {
  status: StepStatus;
  metrics?: StepMetrics;
  // Step results are shown as summaries plus raw JSON; their shapes live in backend/app/schemas.py.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  result?: any;
  error?: string;
  escalations?: Escalation[];
  iterations?: VerifyIteration[];
}

export type CaseEvent =
  | { event: "step_started"; data: { step: StepName; index: number; total: number } }
  | {
      event: "step_completed";
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      data: { step: StepName; index: number; result: any; metrics: StepMetrics };
    }
  | { event: "step_skipped"; data: { step: StepName; index: number; reason: string } }
  | { event: "escalated"; data: Escalation }
  | { event: "verify_iteration"; data: VerifyIteration }
  | { event: "step_failed"; data: { step: StepName; index: number; error: string; metrics: StepMetrics } }
  | { event: "case_failed"; data: { failed_step: StepName | null; totals?: Totals; error?: string } }
  | { event: "case_completed"; data: { package: FinalPackage; totals: Totals; documents?: DocumentLinks | null } };

/** Download paths of the finished files: on the API (/api/...) for live runs, static (/samples/...) for samples. */
export interface DocumentLinks {
  docx: string;
  pdf: string;
}

/** Error body returned by the API when a run is refused (limits, kill switch, busy). */
export interface Refusal {
  error: string;
  message: string;
  retry_after_s: number | null;
}

/** A recorded run: every SSE event with its time offset in seconds. */
export interface SampleRun {
  title: string;
  narrative: string;
  events: (CaseEvent & { t: number })[];
}
