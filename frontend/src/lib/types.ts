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

export interface StepMetrics {
  models: string[];
  llm_calls: number;
  input_tokens: number;
  output_tokens: number;
  llm_latency_s: number;
  latency_s: number;
  cost_usd: number;
  cost_complete: boolean;
  tavily_searches: number;
}

export interface CitationCheck {
  sentencia_id: string;
  estado: "verificada" | "no_verificada";
  url: string | null;
  detalle: string;
}

export interface VerificationReport {
  citas_encontradas: string[];
  citas_verificadas: CitationCheck[];
  citas_no_verificadas: CitationCheck[];
  observaciones: string[];
}

export interface TutelaDraft {
  hechos: string[];
  derechos_vulnerados: string[];
  fundamentos_juridicos: string;
  pretensiones: string[];
  pruebas_y_anexos: string[];
  juramento: string;
  texto_completo: string;
}

export interface FinalPackage {
  draft: TutelaDraft;
  verification: VerificationReport;
  checklist_anexos: string[];
  donde_radicar: string;
  que_hacer_si_no_cumplen: string;
  aviso: string;
}

export interface Totals {
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  latency_s: number;
  tavily_searches: number;
}

export type StepStatus = "pending" | "running" | "done" | "failed";

export interface StepState {
  status: StepStatus;
  metrics?: StepMetrics;
  // Step results are shown as summaries plus raw JSON; their shapes live in backend/app/schemas.py.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  result?: any;
  error?: string;
}

export type CaseEvent =
  | { event: "step_started"; data: { step: StepName; index: number; total: number } }
  | {
      event: "step_completed";
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      data: { step: StepName; index: number; result: any; metrics: StepMetrics };
    }
  | { event: "step_failed"; data: { step: StepName; index: number; error: string; metrics: StepMetrics } }
  | { event: "case_failed"; data: { failed_step: StepName | null; totals?: Totals; error?: string } }
  | { event: "case_completed"; data: { package: FinalPackage; totals: Totals } };
