"use client";

import { useEffect, useRef, useState } from "react";

import demoCases from "@/data/demoCases.json";
import { SAMPLE_RUNS } from "@/data/samples";
import { fetchUsageStatus, replaySample, runCase, RunRefusedError } from "@/lib/sse";
import {
  STEPS,
  STEP_LABELS,
  type CaseEvent,
  type CitationCheck,
  type FinalPackage,
  type Refusal,
  type StepMetrics,
  type StepName,
  type StepState,
  type Totals,
} from "@/lib/types";

const MAX_NARRATIVE_CHARS = 4000;

const initialSteps = (): Record<StepName, StepState> =>
  Object.fromEntries(STEPS.map((s) => [s, { status: "pending" }])) as Record<StepName, StepState>;

const shortModel = (model: string | null | undefined) => (model ? model.split("/").pop() : "");

export default function AmparoApp() {
  const [narrative, setNarrative] = useState("");
  const [steps, setSteps] = useState(initialSteps);
  const [pkg, setPkg] = useState<FinalPackage | null>(null);
  const [totals, setTotals] = useState<Totals | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  const [running, setRunning] = useState(false);
  const [sampleTitle, setSampleTitle] = useState<string | null>(null);
  const [liveEnabled, setLiveEnabled] = useState(true);
  const [selectedDemo, setSelectedDemo] = useState(0);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchUsageStatus(controller.signal).then((status) => {
      if (status && !status.live_runs_enabled) {
        setLiveEnabled(false);
        setRefusal({
          error: "live_runs_disabled",
          message: "Live runs are switched off for now. You can still watch a recorded sample run.",
          retry_after_s: null,
        });
      }
    });
    return () => controller.abort();
  }, []);

  const updateStep = (step: StepName, patch: (prev: StepState) => Partial<StepState>) =>
    setSteps((prev) => ({ ...prev, [step]: { ...prev[step], ...patch(prev[step]) } }));

  function handle({ event, data }: CaseEvent) {
    switch (event) {
      case "step_started":
        updateStep(data.step, () => ({ status: "running" }));
        break;
      case "escalated":
        updateStep(data.step, (s) => ({ escalations: [...(s.escalations ?? []), data] }));
        break;
      case "verify_iteration":
        updateStep("verify", (s) => ({ iterations: [...(s.iterations ?? []), data] }));
        break;
      case "step_completed":
        updateStep(data.step, () => ({ status: "done", result: data.result, metrics: data.metrics }));
        break;
      case "step_failed":
        updateStep(data.step, () => ({ status: "failed", error: data.error, metrics: data.metrics }));
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

  async function play(source: (signal: AbortSignal) => AsyncGenerator<CaseEvent>, sample: string | null) {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setSteps(initialSteps());
    setPkg(null);
    setTotals(null);
    setError(null);
    setRefusal(null);
    setSampleTitle(sample);
    setRunning(true);
    try {
      for await (const event of source(controller.signal)) handle(event);
    } catch (err) {
      if (controller.signal.aborted) return;
      if (err instanceof RunRefusedError) setRefusal(err.refusal);
      else setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (abortRef.current === controller) setRunning(false);
    }
  }

  const startLive = () => play((signal) => runCase(narrative, signal), null);

  function watchSample(index: number) {
    const sample = SAMPLE_RUNS[index];
    setSelectedDemo(index);
    setNarrative(sample.narrative);
    play((signal) => replaySample(sample, signal), sample.title);
  }

  const tooLong = narrative.length > MAX_NARRATIVE_CHARS;

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
          <p className={`mt-1 text-right text-xs ${tooLong ? "text-red-700" : "text-slate-400"}`}>
            {narrative.length.toLocaleString()} / {MAX_NARRATIVE_CHARS.toLocaleString()} characters
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span className="text-xs text-slate-500">Load a synthetic demo case:</span>
            {demoCases.map((c, i) => (
              <button
                key={c.id}
                type="button"
                title={c.title}
                disabled={running}
                onClick={() => {
                  setSelectedDemo(i);
                  setNarrative(c.narrative);
                }}
                className="rounded-md border border-slate-300 px-2.5 py-1 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
              >
                {i + 1}. {c.title}
              </button>
            ))}
          </div>
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={startLive}
              disabled={running || !liveEnabled || tooLong || narrative.trim().length < 20}
              className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
            >
              {running && !sampleTitle ? "Running…" : "Draft my tutela"}
            </button>
            <SampleMenu disabled={running} selected={selectedDemo} onSelect={setSelectedDemo} onWatch={watchSample} />
            {running && (
              <button
                type="button"
                onClick={() => {
                  abortRef.current?.abort();
                  setRunning(false);
                }}
                className="text-sm text-slate-600 underline"
              >
                Cancel
              </button>
            )}
          </div>
          {refusal && (
            <div className="mt-4 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
              <p>{refusal.message}</p>
              <button
                type="button"
                onClick={() => watchSample(selectedDemo)}
                className="mt-2 rounded-md bg-amber-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-800"
              >
                Watch a sample run
              </button>
            </div>
          )}
          {error && (
            <div className="mt-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
              <p>{error}</p>
              {!sampleTitle && (
                <button
                  type="button"
                  onClick={() => watchSample(selectedDemo)}
                  className="mt-2 text-xs font-medium underline"
                >
                  Watch a recorded sample run instead
                </button>
              )}
            </div>
          )}
          {sampleTitle && (
            <p className="mt-4 rounded-md border border-sky-200 bg-sky-50 p-3 text-sm text-sky-900">
              Replaying a recorded run of the synthetic case “{sampleTitle}”. No tokens are spent; the models, times
              and costs shown are those of the original run.
            </p>
          )}
        </section>

        <Timeline steps={steps} totals={totals} />
      </div>

      {pkg && <Results pkg={pkg} />}
    </div>
  );
}

function SampleMenu({
  disabled,
  selected,
  onSelect,
  onWatch,
}: {
  disabled: boolean;
  selected: number;
  onSelect: (index: number) => void;
  onWatch: (index: number) => void;
}) {
  return (
    <span className="flex items-center gap-1.5">
      <button
        type="button"
        disabled={disabled}
        onClick={() => onWatch(selected)}
        className="rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-800 hover:bg-slate-50 disabled:opacity-50"
      >
        Watch a sample run
      </button>
      <select
        aria-label="Sample case"
        value={selected}
        disabled={disabled}
        onChange={(e) => onSelect(Number(e.target.value))}
        className="max-w-40 rounded-md border border-slate-300 bg-white px-1.5 py-2 text-xs text-slate-700"
      >
        {SAMPLE_RUNS.map((s, i) => (
          <option key={i} value={i}>
            {i + 1}. {s.title}
          </option>
        ))}
      </select>
    </span>
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
                {s.escalations?.map((e, k) => (
                  <p key={k} className="mt-1 rounded bg-violet-50 px-1.5 py-1 text-[11px] text-violet-900">
                    Escalated {shortModel(e.from_model)} → {shortModel(e.to_model)}: {e.reasons.join("; ")}
                  </p>
                ))}
                {s.iterations
                  ?.filter((it) => it.phase === "done")
                  .map((it) => (
                    <p key={it.iteration} className="mt-1 rounded bg-slate-50 px-1.5 py-1 text-[11px] text-slate-700">
                      Check loop {it.iteration}: {it.found?.length ? `found ${it.found.join(", ")}` : "none found"}
                      {it.removed?.length ? ` · rewrote without ${it.removed.join(", ")}` : ""}
                    </p>
                  ))}
                {s.status === "running" && s.iterations?.at(-1)?.phase === "searching" && (
                  <p className="mt-1 text-[11px] text-blue-700">
                    Searching for {s.iterations.at(-1)?.unverified?.join(", ")}…
                  </p>
                )}
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
  const others = metrics.models.filter((m) => m !== metrics.final_model).map(shortModel);
  return (
    <p className="mt-1 text-[11px] text-slate-500">
      {metrics.final_model ? (
        <span title="Model that produced the final output of this step" className="font-medium text-slate-700">
          {shortModel(metrics.final_model)}
        </span>
      ) : (
        "no LLM"
      )}
      {others.length > 0 && ` (also ${others.join(", ")})`} · {metrics.latency_s.toFixed(1)} s · $
      {metrics.cost_usd.toFixed(4)}
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
      if (result.citas_no_verificadas_iniciales?.length)
        text += ` (${result.citas_no_verificadas_iniciales.length} unverified before the check loop)`;
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
          {pkg.verification.ciclo.length > 0 && (
            <>
              <h3 className="mb-1 mt-4 text-xs font-semibold text-slate-700">Check loop</h3>
              <ul className="space-y-1 text-xs text-slate-600">
                {pkg.verification.ciclo.map((o) => (
                  <li key={o.sentencia_id}>
                    <span className="font-medium">{o.sentencia_id}</span>:{" "}
                    {o.resultado === "verificada_busqueda_dirigida" ? "found by a targeted search" : "removed from the text"}{" "}
                    (loop {o.iteracion})
                  </li>
                ))}
              </ul>
            </>
          )}
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
