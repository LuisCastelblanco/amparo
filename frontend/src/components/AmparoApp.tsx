"use client";

import { useRef, useState } from "react";

import demoCases from "@/data/demoCases.json";
import { runCase } from "@/lib/sse";
import {
  STEPS,
  STEP_LABELS,
  type CitationCheck,
  type FinalPackage,
  type StepMetrics,
  type StepName,
  type StepState,
  type Totals,
} from "@/lib/types";

const initialSteps = (): Record<StepName, StepState> =>
  Object.fromEntries(STEPS.map((s) => [s, { status: "pending" }])) as Record<StepName, StepState>;

export default function AmparoApp() {
  const [narrative, setNarrative] = useState("");
  const [steps, setSteps] = useState(initialSteps);
  const [pkg, setPkg] = useState<FinalPackage | null>(null);
  const [totals, setTotals] = useState<Totals | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const updateStep = (step: StepName, patch: Partial<StepState>) =>
    setSteps((prev) => ({ ...prev, [step]: { ...prev[step], ...patch } }));

  async function start() {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setSteps(initialSteps());
    setPkg(null);
    setTotals(null);
    setError(null);
    setRunning(true);
    try {
      for await (const { event, data } of runCase(narrative, controller.signal)) {
        switch (event) {
          case "step_started":
            updateStep(data.step, { status: "running" });
            break;
          case "step_completed":
            updateStep(data.step, { status: "done", result: data.result, metrics: data.metrics });
            break;
          case "step_failed":
            updateStep(data.step, { status: "failed", error: data.error, metrics: data.metrics });
            setError(`Step "${STEP_LABELS[data.step].title}" failed: ${data.error}`);
            break;
          case "case_failed":
            if (data.totals) setTotals(data.totals);
            if (data.error) setError(data.error);
            break;
          case "case_completed":
            setPkg(data.package);
            setTotals(data.totals);
            break;
        }
      }
    } catch (err) {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-8 sm:px-6">
      <header className="mb-6">
        <h1 className="text-3xl font-semibold tracking-tight">Amparo</h1>
        <p className="mt-1 text-slate-600">
          Draft an <em>acción de tutela</em> when your EPS denies a health service.
        </p>
        <p className="mt-3 inline-block rounded-md border border-amber-300 bg-amber-50 px-3 py-1.5 text-sm font-medium text-amber-900">
          Drafting aid, not legal advice. Review the document before filing it.
        </p>
      </header>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
        <section className="rounded-lg border border-slate-200 bg-white p-4">
          <label htmlFor="narrative" className="mb-2 block text-sm font-medium text-slate-700">
            Tell your case in your own words (in Spanish)
          </label>
          <textarea
            id="narrative"
            value={narrative}
            onChange={(e) => setNarrative(e.target.value)}
            rows={12}
            placeholder="Ej.: Mi EPS me negó el medicamento que me formuló el médico porque..."
            className="w-full resize-y rounded-md border border-slate-300 p-3 text-sm leading-relaxed focus:border-slate-500 focus:outline-none"
          />
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <span className="text-xs text-slate-500">Load a synthetic demo case:</span>
            {demoCases.map((c, i) => (
              <button
                key={c.id}
                type="button"
                title={c.title}
                disabled={running}
                onClick={() => setNarrative(c.narrative)}
                className="rounded-md border border-slate-300 px-2.5 py-1 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
              >
                {i + 1}. {c.title}
              </button>
            ))}
          </div>
          <div className="mt-4 flex items-center gap-3">
            <button
              type="button"
              onClick={start}
              disabled={running || narrative.trim().length < 20}
              className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
            >
              {running ? "Running…" : "Draft my tutela"}
            </button>
            {running && (
              <button
                type="button"
                onClick={() => abortRef.current?.abort()}
                className="text-sm text-slate-600 underline"
              >
                Cancel
              </button>
            )}
          </div>
          {error && (
            <p className="mt-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</p>
          )}
        </section>

        <Timeline steps={steps} totals={totals} />
      </div>

      {pkg && <Results pkg={pkg} />}
    </div>
  );
}

function Timeline({ steps, totals }: { steps: Record<StepName, StepState>; totals: Totals | null }) {
  return (
    <aside className="rounded-lg border border-slate-200 bg-white p-4">
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">Pipeline</h2>
      <ol className="space-y-3">
        {STEPS.map((name, i) => {
          const s = steps[name];
          return (
            <li key={name} className="flex gap-3">
              <StatusDot status={s.status} index={i + 1} />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium">{STEP_LABELS[name].title}</p>
                <p className="text-xs text-slate-500">{STEP_LABELS[name].description}</p>
                {s.metrics && <MetricsLine metrics={s.metrics} />}
                {s.status === "done" && <StepSummary step={name} result={s.result} />}
                {s.status === "failed" && <p className="mt-1 text-xs text-red-700">{s.error}</p>}
                {s.result && (
                  <details className="mt-1">
                    <summary className="cursor-pointer text-xs text-slate-500">Raw output</summary>
                    <pre className="mt-1 max-h-60 overflow-auto rounded bg-slate-50 p-2 text-[11px] leading-snug">
                      {JSON.stringify(s.result, null, 2)}
                    </pre>
                  </details>
                )}
              </div>
            </li>
          );
        })}
      </ol>
      {totals && (
        <div className="mt-4 border-t border-slate-200 pt-3 text-xs text-slate-600">
          <p className="font-medium text-slate-800">Total</p>
          <p>
            {totals.latency_s.toFixed(1)} s · ${totals.cost_usd.toFixed(4)} · {totals.input_tokens.toLocaleString()} in /{" "}
            {totals.output_tokens.toLocaleString()} out tokens · {totals.tavily_searches} Tavily searches
          </p>
        </div>
      )}
    </aside>
  );
}

function StatusDot({ status, index }: { status: StepState["status"]; index: number }) {
  const styles = {
    pending: "border-slate-300 text-slate-400",
    running: "border-blue-500 text-blue-600 animate-pulse",
    done: "border-emerald-500 bg-emerald-500 text-white",
    failed: "border-red-500 bg-red-500 text-white",
  }[status];
  return (
    <span
      className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border-2 text-xs font-semibold ${styles}`}
    >
      {status === "done" ? "✓" : status === "failed" ? "!" : index}
    </span>
  );
}

function MetricsLine({ metrics }: { metrics: StepMetrics }) {
  const models = metrics.models.map((m) => m.split("/").pop()).join(", ");
  return (
    <p className="mt-1 text-[11px] text-slate-500">
      {models || "no LLM"} · {metrics.latency_s.toFixed(1)} s · ${metrics.cost_usd.toFixed(4)}
      {metrics.tavily_searches > 0 && ` · ${metrics.tavily_searches} Tavily`}
    </p>
  );
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function StepSummary({ step, result }: { step: StepName; result: any }) {
  let text: string | null = null;
  switch (step) {
    case "intake":
      text = `${result.eps} · ${result.servicio_negado}`;
      if (result.datos_faltantes?.length) text += ` · ${result.datos_faltantes.length} open question(s)`;
      break;
    case "analysis":
      text = `Urgency: ${result.urgencia} · ${result.procedencia_tutela.procede ? "admissible" : "may not be admissible"}`;
      break;
    case "research":
      text = `${result.precedentes.length} relevant ruling(s)`;
      break;
    case "verify":
      text = `${result.citas_verificadas.length} verified · ${result.citas_no_verificadas.length} unverified`;
      break;
  }
  return text ? <p className="mt-1 text-xs text-slate-700">{text}</p> : null;
}

function Results({ pkg }: { pkg: FinalPackage }) {
  const [copied, setCopied] = useState(false);
  const citations: CitationCheck[] = [...pkg.verification.citas_verificadas, ...pkg.verification.citas_no_verificadas];

  async function copy() {
    await navigator.clipboard.writeText(pkg.draft.texto_completo);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <div className="mt-8 grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
      <section className="rounded-lg border border-slate-200 bg-white p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-lg font-semibold">Draft tutela (Spanish)</h2>
          <button
            type="button"
            onClick={copy}
            className="rounded-md border border-slate-300 px-2.5 py-1 text-xs hover:bg-slate-50"
          >
            {copied ? "Copied" : "Copy text"}
          </button>
        </div>
        <pre lang="es" className="whitespace-pre-wrap font-serif text-[15px] leading-relaxed text-slate-900">
          {pkg.draft.texto_completo}
        </pre>
        <p className="mt-4 rounded-md bg-slate-50 p-3 text-xs text-slate-600" lang="es">
          {pkg.aviso}
        </p>
      </section>

      <div className="space-y-6">
        <section className="rounded-lg border border-slate-200 bg-white p-4">
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500">Citations</h2>
          {citations.length === 0 && <p className="text-sm text-slate-600">No rulings cited.</p>}
          <ul className="space-y-2">
            {citations.map((c) => (
              <li key={c.sentencia_id} className="text-sm">
                <div className="flex items-center gap-2">
                  <span
                    className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${
                      c.estado === "verificada" ? "bg-emerald-100 text-emerald-800" : "bg-red-100 text-red-800"
                    }`}
                  >
                    {c.estado === "verificada" ? "Verified" : "Not verified"}
                  </span>
                  {c.url ? (
                    <a href={c.url} target="_blank" rel="noreferrer" className="font-medium underline">
                      {c.sentencia_id}
                    </a>
                  ) : (
                    <span className="font-medium">{c.sentencia_id}</span>
                  )}
                </div>
                <p className="mt-0.5 text-xs text-slate-500" lang="es">
                  {c.detalle}
                </p>
              </li>
            ))}
          </ul>
          {pkg.verification.observaciones.length > 0 && (
            <>
              <h3 className="mb-1 mt-4 text-xs font-semibold text-slate-700">Review notes</h3>
              <ul className="list-disc space-y-1 pl-4 text-xs text-slate-600" lang="es">
                {pkg.verification.observaciones.map((o, i) => (
                  <li key={i}>{o}</li>
                ))}
              </ul>
            </>
          )}
        </section>

        <section className="rounded-lg border border-slate-200 bg-white p-4">
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500">Annex checklist</h2>
          <ul className="space-y-1 text-sm" lang="es">
            {pkg.checklist_anexos.map((item, i) => (
              <li key={i} className="flex gap-2">
                <input type="checkbox" className="mt-1" aria-label={item} />
                <span>{item.replace(/^\d+\.\s*/, "")}</span>
              </li>
            ))}
          </ul>
        </section>

        <section className="rounded-lg border border-slate-200 bg-white p-4">
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500">Where to file</h2>
          <p className="text-sm leading-relaxed" lang="es">
            {pkg.donde_radicar}
          </p>
          <h3 className="mb-1 mt-4 text-xs font-semibold text-slate-700">If the EPS does not comply</h3>
          <p className="text-sm leading-relaxed" lang="es">
            {pkg.que_hacer_si_no_cumplen}
          </p>
        </section>
      </div>
    </div>
  );
}
