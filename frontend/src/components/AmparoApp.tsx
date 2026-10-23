"use client";

import { useEffect, useRef, useState } from "react";

import Intro from "@/components/Intro";
import QuestionsPanel, { type PendingQuestion } from "@/components/QuestionsPanel";
import Results, { modelLabel } from "@/components/Results";
import demoCases from "@/data/demoCases.json";
import { SAMPLE_RUNS } from "@/data/samples";
import { checkIntake, checkJudgeCode, fetchUsageStatus, replaySample, runCase, RunRefusedError } from "@/lib/sse";
import {
  STEPS,
  STEP_LABELS,
  type CaseEvent,
  type DocumentLinks,
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

const focusRing = "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-900";

const JUDGE_CODE_KEY = "amparo-judge-code";

export default function AmparoApp() {
  const [narrative, setNarrative] = useState("");
  const [englishSummary, setEnglishSummary] = useState(false);
  const [steps, setSteps] = useState(initialSteps);
  const [pkg, setPkg] = useState<FinalPackage | null>(null);
  const [totals, setTotals] = useState<Totals | null>(null);
  const [documents, setDocuments] = useState<DocumentLinks | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  const [running, setRunning] = useState(false);
  const [sampleTitle, setSampleTitle] = useState<string | null>(null);
  const [liveEnabled, setLiveEnabled] = useState(true);
  const [selectedDemo, setSelectedDemo] = useState(0);
  // null: not checked yet; []: checked, nothing missing.
  const [questions, setQuestions] = useState<PendingQuestion[] | null>(null);
  const [checking, setChecking] = useState(false);
  const [judgeCode, setJudgeCode] = useState("");
  // null: not checked (or the API was unreachable).
  const [judgeValid, setJudgeValid] = useState<boolean | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Judge access: ?code=... (removed from the address bar so it is not shared by accident) or a code
  // typed earlier in this tab.
  useEffect(() => {
    const url = new URL(window.location.href);
    let code = url.searchParams.get("code")?.trim() ?? "";
    if (code) {
      url.searchParams.delete("code");
      window.history.replaceState(null, "", url.pathname + url.search + url.hash);
    } else {
      try {
        code = sessionStorage.getItem(JUDGE_CODE_KEY) ?? "";
      } catch {
        // storage unavailable: the field starts empty
      }
    }
    if (code) applyJudgeCode(code);
  }, []);

  async function applyJudgeCode(code: string) {
    setJudgeCode(code);
    setJudgeValid(null);
    try {
      if (code) sessionStorage.setItem(JUDGE_CODE_KEY, code);
      else sessionStorage.removeItem(JUDGE_CODE_KEY);
    } catch {
      // storage unavailable: the code still works for this page load
    }
    if (code) setJudgeValid(await checkJudgeCode(code));
  }

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

  // ?sample=1|2|3 replays that recorded run on load (a direct link for reviewers).
  useEffect(() => {
    const index = Number(new URLSearchParams(window.location.search).get("sample")) - 1;
    if (index >= 0 && index < SAMPLE_RUNS.length) watchSample(index);
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
        setDocuments(data.documents ?? null);
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
    setDocuments(null);
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

  const editNarrative = (text: string) => {
    setNarrative(text);
    setQuestions(null);
  };

  function runLive(pending: PendingQuestion[]) {
    const answers = pending
      .filter((q) => !q.skipped && q.answer.trim())
      .map((q) => ({ question: q.question, answer: q.answer.trim() }));
    setQuestions(null);
    play((signal) => runCase(narrative, { englishSummary, answers, judgeCode }, signal), null);
  }

  /** Ask the clarifying questions first, if the story misses something critical; then draft. */
  async function startLive() {
    if (questions !== null) return runLive(questions);
    setChecking(true);
    setError(null);
    setRefusal(null);
    try {
      const { questions: asked } = await checkIntake(narrative, judgeCode);
      if (asked.length > 0) {
        setQuestions(asked.map((question) => ({ question, answer: "", skipped: false })));
        return;
      }
    } catch (err) {
      if (err instanceof RunRefusedError) {
        setRefusal(err.refusal);
        return;
      }
      // The check is optional: if it fails, draft without questions.
    } finally {
      setChecking(false);
    }
    runLive([]);
  }

  function watchSample(index: number) {
    const sample = SAMPLE_RUNS[index];
    setSelectedDemo(index);
    editNarrative(sample.narrative);
    play((signal) => replaySample(sample, signal), sample.title);
  }

  const tooLong = narrative.length > MAX_NARRATIVE_CHARS;
  const current = STEPS.find((s) => steps[s].status === "running");

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-8 sm:px-6">
      <header className="mb-6">
        <h1 className="text-3xl font-semibold tracking-tight">Amparo</h1>
        <p className="mt-1 text-slate-700">
          Draft an <em>acción de tutela</em> when your EPS (health insurer) denies a health service in Colombia.
        </p>
        <p className="mt-3 inline-block rounded-md border border-amber-300 bg-amber-50 px-3 py-1.5 text-sm font-medium text-amber-900">
          Drafting aid, not legal advice. Review the document before filing it.
        </p>
      </header>

      <Intro />

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
        <section aria-labelledby="case-heading" className="min-w-0 rounded-lg border border-slate-200 bg-white p-4">
          <h2 id="case-heading" className="sr-only">
            Your case
          </h2>
          <label htmlFor="narrative" className="mb-2 block text-sm font-medium text-slate-800">
            Tell your case in your own words (in Spanish)
          </label>
          <textarea
            id="narrative"
            value={narrative}
            onChange={(e) => editNarrative(e.target.value)}
            rows={10}
            aria-describedby="narrative-count"
            placeholder="Ej.: Mi EPS me negó el medicamento que me formuló el médico porque..."
            className="w-full resize-y rounded-md border border-slate-400 p-3 text-sm leading-relaxed placeholder:text-slate-500 focus:border-slate-700 focus:outline-none focus:ring-2 focus:ring-slate-300"
          />
          <p
            id="narrative-count"
            className={`mt-1 text-right text-xs ${tooLong ? "font-medium text-red-700" : "text-slate-600"}`}
          >
            {narrative.length.toLocaleString("en-US")} / {MAX_NARRATIVE_CHARS.toLocaleString("en-US")} characters
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span className="text-xs text-slate-600">Load a synthetic demo case:</span>
            {demoCases.map((c, i) => (
              <button
                key={c.id}
                type="button"
                title={c.title}
                disabled={running}
                onClick={() => {
                  setSelectedDemo(i);
                  editNarrative(c.narrative);
                }}
                className={`max-w-full truncate rounded-md border border-slate-300 px-2.5 py-1 text-xs text-slate-800 hover:bg-slate-50 disabled:opacity-60 ${focusRing}`}
              >
                {i + 1}. {c.title}
              </button>
            ))}
          </div>
          <label className="mt-4 flex items-start gap-2 text-sm text-slate-800">
            <input
              type="checkbox"
              checked={englishSummary}
              onChange={(e) => setEnglishSummary(e.target.checked)}
              className="mt-0.5 h-4 w-4"
            />
            <span>
              English summary for reviewers
              <span className="block text-xs text-slate-600">
                One extra call to the fast model after the draft. Sample runs already include it.
              </span>
            </span>
          </label>
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={startLive}
              disabled={running || checking || !liveEnabled || tooLong || narrative.trim().length < 20}
              className={`rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-60 ${focusRing}`}
            >
              {checking ? "Reading your story…" : running && !sampleTitle ? "Running…" : "Draft my tutela"}
            </button>
            <SampleMenu disabled={running || checking} selected={selectedDemo} onSelect={setSelectedDemo} onWatch={watchSample} />
            {running && (
              <button
                type="button"
                onClick={() => {
                  abortRef.current?.abort();
                  setRunning(false);
                }}
                className={`text-sm text-slate-700 underline ${focusRing}`}
              >
                Cancel
              </button>
            )}
          </div>
          <JudgeAccess key={judgeCode} code={judgeCode} valid={judgeValid} disabled={running || checking} onApply={applyJudgeCode} />
          {questions && questions.length > 0 && (
            <QuestionsPanel
              questions={questions}
              onChange={setQuestions}
              onContinue={() => runLive(questions)}
              disabled={running}
            />
          )}
          {refusal && (
            <div role="status" className="mt-4 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
              <p>{refusal.message}</p>
              <button
                type="button"
                onClick={() => watchSample(selectedDemo)}
                className={`mt-2 rounded-md bg-amber-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-800 ${focusRing}`}
              >
                Watch a sample run
              </button>
            </div>
          )}
          {error && (
            <div role="alert" className="mt-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
              <p className="break-words">{error}</p>
              {!sampleTitle && (
                <button
                  type="button"
                  onClick={() => watchSample(selectedDemo)}
                  className={`mt-2 text-xs font-medium underline ${focusRing}`}
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
      <p className="sr-only" aria-live="polite">
        {current ? `Running step: ${STEP_LABELS[current].title}` : pkg ? "Your tutela is ready." : ""}
      </p>

      {pkg && <Results pkg={pkg} steps={steps} totals={totals} documents={documents} />}
    </div>
  );
}

/** Discreet field for the hackathon judges' access code, which lifts the public demo limits. */
function JudgeAccess({
  code,
  valid,
  disabled,
  onApply,
}: {
  code: string;
  valid: boolean | null;
  disabled: boolean;
  onApply: (code: string) => void;
}) {
  // The parent remounts this component (key) when the applied code changes, which resets the draft.
  const [draft, setDraft] = useState(code);
  const active = Boolean(code) && valid === true;
  return (
    <details className="mt-3 text-xs text-slate-700" open={Boolean(code) || undefined}>
      <summary className={`cursor-pointer select-none text-slate-600 ${focusRing}`}>
        {active ? "Judge access active" : "Judge access code"}
      </summary>
      <form
        className="mt-2 flex flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          onApply(draft.trim());
        }}
      >
        <label htmlFor="judge-code" className="sr-only">
          Judge access code
        </label>
        <input
          id="judge-code"
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          disabled={disabled}
          aria-describedby="judge-code-status"
          className="min-w-0 flex-1 rounded-md border border-slate-400 px-2 py-1.5 font-mono text-xs sm:max-w-64"
        />
        <button
          type="submit"
          disabled={disabled || draft.trim() === code}
          className={`rounded-md border border-slate-400 px-2.5 py-1.5 text-xs text-slate-900 hover:bg-slate-50 disabled:opacity-60 ${focusRing}`}
        >
          {draft.trim() || !code ? "Apply" : "Clear"}
        </button>
      </form>
      <p id="judge-code-status" role="status" className="mt-1">
        {!code
          ? "For hackathon judges: lifts the public demo limits."
          : valid === true
            ? "Code accepted: public demo limits do not apply to your runs."
            : valid === false
              ? "This code is not valid. Clear it to use the public demo."
              : "Checking the code…"}
      </p>
    </details>
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
    <span className="flex min-w-0 max-w-full items-center gap-1.5">
      <button
        type="button"
        disabled={disabled}
        onClick={() => onWatch(selected)}
        className={`shrink-0 rounded-md border border-slate-400 px-3 py-2 text-sm text-slate-900 hover:bg-slate-50 disabled:opacity-60 ${focusRing}`}
      >
        Watch a sample run
      </button>
      <select
        aria-label="Sample case to watch"
        value={selected}
        disabled={disabled}
        onChange={(e) => onSelect(Number(e.target.value))}
        className={`min-w-0 max-w-40 rounded-md border border-slate-400 bg-white px-1.5 py-2 text-xs text-slate-800 ${focusRing}`}
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
    <aside aria-labelledby="pipeline-heading" className="min-w-0 rounded-lg border border-slate-200 bg-white p-4">
      <h2 id="pipeline-heading" className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-600">
        Pipeline
      </h2>
      <ol className="space-y-3">
        {STEPS.map((name, i) => {
          const s = steps[name];
          return (
            <li key={name} className="flex gap-3">
              <StatusDot status={s.status} index={i + 1} />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium">{STEP_LABELS[name].title}</p>
                <p className="text-xs text-slate-600">{STEP_LABELS[name].description}</p>
                {s.metrics && <MetricsLine metrics={s.metrics} />}
                {s.escalations?.map((e, k) => (
                  <p key={k} className="mt-1 break-words rounded bg-violet-50 px-1.5 py-1 text-[11px] text-violet-900">
                    Escalated {modelLabel(e.from_model)} → {modelLabel(e.to_model)}: {e.reasons.join("; ")}
                  </p>
                ))}
                {s.metrics?.notes?.map((note, k) => (
                  <p key={k} className="mt-1 break-words rounded bg-slate-50 px-1.5 py-1 text-[11px] text-slate-700">
                    Corrected without a model: {note}
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
                  <p className="mt-1 text-[11px] text-blue-800">
                    Searching for {s.iterations.at(-1)?.unverified?.join(", ")}…
                  </p>
                )}
                {s.status === "done" && <StepSummary step={name} result={s.result} />}
                {s.status === "failed" && <p className="mt-1 break-words text-xs text-red-700">{s.error}</p>}
                {s.result && (
                  <details className="mt-1">
                    <summary className="cursor-pointer text-xs text-slate-600">Raw output</summary>
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
        <div className="mt-4 border-t border-slate-200 pt-3 text-xs text-slate-700">
          <p className="font-medium text-slate-900">Total</p>
          <p>
            {totals.latency_s.toFixed(1)} s · ${totals.cost_usd.toFixed(4)} ·{" "}
            {totals.input_tokens.toLocaleString("en-US")} in / {totals.output_tokens.toLocaleString("en-US")} out
            tokens · {totals.tavily_searches} Tavily searches
          </p>
        </div>
      )}
    </aside>
  );
}

function StatusDot({ status, index }: { status: StepState["status"]; index: number }) {
  const styles = {
    pending: "border-slate-400 text-slate-600",
    running: "border-blue-600 text-blue-700 animate-pulse",
    done: "border-emerald-600 bg-emerald-600 text-white",
    failed: "border-red-600 bg-red-600 text-white",
  }[status];
  const label = { pending: "pending", running: "running", done: "done", failed: "failed" }[status];
  return (
    <span
      role="img"
      aria-label={`Step ${index}: ${label}`}
      className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border-2 text-xs font-semibold ${styles}`}
    >
      {status === "done" ? "✓" : status === "failed" ? "!" : index}
    </span>
  );
}

function MetricsLine({ metrics }: { metrics: StepMetrics }) {
  const others = metrics.models.filter((m) => m !== metrics.final_model).map(modelLabel);
  return (
    <p className="mt-1 text-[11px] text-slate-600">
      {metrics.final_model ? (
        <span title="Model that produced the final output of this step" className="font-medium text-slate-800">
          {modelLabel(metrics.final_model)}
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
      text = `${result.eps} · ${result.servicio_negado}${result.ciudad ? ` · ${result.ciudad}` : ""}`;
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
    case "package":
      if (result.canal_radicacion)
        text = result.canal_radicacion.encontrado
          ? `Official filing channel found for ${result.canal_radicacion.ciudad}`
          : `No specific filing channel found for ${result.canal_radicacion.ciudad}`;
      break;
  }
  return text ? <p className="mt-1 break-words text-xs text-slate-800">{text}</p> : null;
}
