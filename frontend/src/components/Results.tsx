"use client";

import { useState } from "react";

import { documentUrl, downloadDocument } from "@/lib/sse";
import {
  STEPS,
  STEP_LABELS,
  type DocumentLinks,
  type FilingChannel,
  type FinalPackage,
  type NextSteps,
  type ReviewerSummary,
  type StepName,
  type StepState,
  type Totals,
  type TutelaDraft,
  type VerificationReport,
} from "@/lib/types";

/** "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B" -> "Nemotron 3 Nano" (display only). */
export function modelLabel(model: string | null | undefined): string {
  if (!model) return "";
  const name = model.split("/").pop() ?? model;
  const match = name.match(/nemotron[-_ ]?(\d+)?[-_ ]?(nano|super|ultra)/i);
  if (!match) return name;
  const size = match[2][0].toUpperCase() + match[2].slice(1).toLowerCase();
  return `Nemotron${match[1] ? ` ${match[1]}` : ""} ${size}`;
}

const host = (url: string) => {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
};

/** A package with a draft (the tutela is admissible). */
type DraftedPackage = FinalPackage & { draft: TutelaDraft; verification: VerificationReport };

const card = "rounded-lg border border-slate-200 bg-white p-4";
const cardTitle = "mb-2 text-sm font-semibold uppercase tracking-wide text-slate-600";

export default function Results({
  pkg,
  steps,
  totals,
  documents,
}: {
  pkg: FinalPackage;
  steps: Record<StepName, StepState>;
  totals: Totals | null;
  documents: DocumentLinks | null;
}) {
  if (!pkg.draft || !pkg.verification) {
    return (
      <div className="mt-8 space-y-6">
        {pkg.orientacion && <NotYetCard steps={pkg.orientacion} aviso={pkg.aviso} />}
        <div className="grid gap-6 lg:grid-cols-2">
          <RoutingPanel steps={steps} totals={totals} />
        </div>
      </div>
    );
  }
  const drafted = pkg as DraftedPackage;
  return (
    <div className="mt-8 space-y-6">
      <DocumentCard pkg={drafted} documents={documents} />
      <div className="grid gap-6 lg:grid-cols-2">
        <CitationsCard pkg={drafted} />
        <ChecklistCard items={pkg.checklist_anexos} />
        <FilingCard pkg={pkg} />
        <RoutingPanel steps={steps} totals={totals} />
        <SummaryCard summary={pkg.resumen_revisores} />
      </div>
    </div>
  );
}

function NotYetCard({ steps, aviso }: { steps: NextSteps; aviso: string }) {
  return (
    <section aria-labelledby="notyet-heading" className={`${card} border-amber-300`}>
      <h2 id="notyet-heading" className="text-lg font-semibold">
        A tutela is not the right tool yet
      </h2>
      <p className="text-xs text-slate-600">
        No document was drafted: a judge would most likely reject it now. This is what to do first (in Spanish).
      </p>
      <div lang="es" className="mt-3 space-y-3 text-sm leading-relaxed">
        <p>{steps.por_que_no_ahora}</p>
        <div>
          <h3 className="text-xs font-semibold text-slate-800">Qué hacer primero</h3>
          <ol className="mt-1 list-decimal space-y-1 pl-5">
            {steps.pasos.map((p, i) => (
              <li key={i}>{p.replace(/^\d+[.)]\s*/, "")}</li>
            ))}
          </ol>
        </div>
        <div>
          <h3 className="text-xs font-semibold text-slate-800">Cuándo sí procede la tutela</h3>
          <p>{steps.cuando_procede}</p>
        </div>
      </div>
      <p className="mt-4 rounded-md bg-amber-50 p-3 text-xs text-amber-900" lang="es">
        {aviso}
      </p>
    </section>
  );
}

function DocumentCard({ pkg, documents }: { pkg: DraftedPackage; documents: DocumentLinks | null }) {
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState<"docx" | "pdf" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function copy() {
    await navigator.clipboard.writeText(pkg.draft.texto_completo);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  async function download(format: "docx" | "pdf") {
    setBusy(format);
    setError(null);
    try {
      await downloadDocument(pkg.draft, format);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  const button =
    "rounded-md px-3 py-2 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-900 disabled:opacity-60";
  return (
    <section aria-labelledby="document-heading" className={card}>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 id="document-heading" className="text-lg font-semibold">
            Your tutela <span className="font-normal text-slate-600">(in Spanish)</span>
          </h2>
          <p className="text-xs text-slate-600">
            Fill in the highlighted <span lang="es">[COMPLETAR]</span> fields, sign it and file it.
            {documents?.docx.startsWith("/api/") && " Download links expire after 7 days."}
            {pkg.resumen_revisores && (
              <>
                {" "}
                <a href="#summary-heading" className="underline">
                  English summary below
                </a>
                .
              </>
            )}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {documents ? (
            // Plain links to files the API stored when the case finished: mobile browsers download them
            // directly (Content-Disposition: attachment), with no script between the tap and the file.
            <>
              <a href={documentUrl(documents.docx)} download className={`${button} bg-slate-900 text-white hover:bg-slate-700`}>
                Download .docx
              </a>
              <a href={documentUrl(documents.pdf)} download className={`${button} bg-slate-900 text-white hover:bg-slate-700`}>
                Download PDF
              </a>
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={() => download("docx")}
                disabled={busy !== null}
                className={`${button} bg-slate-900 text-white hover:bg-slate-700`}
              >
                {busy === "docx" ? "Preparing…" : "Download .docx"}
              </button>
              <button
                type="button"
                onClick={() => download("pdf")}
                disabled={busy !== null}
                className={`${button} bg-slate-900 text-white hover:bg-slate-700`}
              >
                {busy === "pdf" ? "Preparing…" : "Download PDF"}
              </button>
            </>
          )}
          <button
            type="button"
            onClick={copy}
            className={`${button} border border-slate-300 text-slate-800 hover:bg-slate-50`}
          >
            {copied ? "Copied" : "Copy text"}
          </button>
        </div>
      </div>
      {error && (
        <p role="alert" className="mb-3 rounded-md border border-red-200 bg-red-50 p-2 text-sm text-red-800">
          {error}
        </p>
      )}
      <div className="max-h-[70vh] overflow-y-auto rounded-md border border-slate-100 bg-slate-50/60 p-4">
        <pre lang="es" className="whitespace-pre-wrap break-words font-serif text-[15px] leading-relaxed text-slate-900">
          <Highlighted text={pkg.draft.texto_completo} />
        </pre>
      </div>
      <p className="mt-4 rounded-md bg-amber-50 p-3 text-xs text-amber-900" lang="es">
        {pkg.aviso}
      </p>
    </section>
  );
}

function Highlighted({ text }: { text: string }) {
  const parts = text.split(/(\[COMPLETAR[^\]]*\])/g);
  return (
    <>
      {parts.map((part, i) =>
        part.startsWith("[COMPLETAR") ? (
          <mark key={i} className="rounded bg-yellow-200 px-0.5 font-sans text-[13px] font-semibold text-slate-900">
            {part}
          </mark>
        ) : (
          part
        ),
      )}
    </>
  );
}

function CitationsCard({ pkg }: { pkg: DraftedPackage }) {
  const v = pkg.verification;
  return (
    <section aria-labelledby="citations-heading" className={card}>
      <h2 id="citations-heading" className={cardTitle}>
        Verified citations
      </h2>
      {v.citas_verificadas.length === 0 && <p className="text-sm text-slate-700">No rulings cited.</p>}
      <ul className="space-y-3">
        {[...v.citas_verificadas, ...v.citas_no_verificadas].map((c) => (
          <li key={c.sentencia_id} className="text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <span
                className={`rounded px-1.5 py-0.5 text-[11px] font-semibold ${
                  c.estado === "verificada" ? "bg-emerald-100 text-emerald-900" : "bg-red-100 text-red-900"
                }`}
              >
                {c.estado === "verificada" ? "Verified" : "Not verified"}
              </span>
              <span className="font-medium">Sentencia {c.sentencia_id}</span>
              {c.url && (
                <a href={c.url} target="_blank" rel="noreferrer" className="break-all text-xs text-slate-700 underline">
                  {host(c.url)}
                  <span className="sr-only"> (source of {c.sentencia_id}, opens in a new tab)</span>
                </a>
              )}
            </div>
            <p className="mt-0.5 text-xs text-slate-600" lang="es">
              {c.detalle}
            </p>
          </li>
        ))}
      </ul>
      {v.ciclo.length > 0 && (
        <>
          <h3 className="mb-1 mt-4 text-xs font-semibold text-slate-800">Check loop</h3>
          <ul className="space-y-1 text-xs text-slate-700">
            {v.ciclo.map((o) => (
              <li key={o.sentencia_id}>
                <span className="font-medium">{o.sentencia_id}</span>:{" "}
                {o.resultado === "verificada_busqueda_dirigida" ? "found by a targeted search" : "removed from the text"}{" "}
                (loop {o.iteracion})
              </li>
            ))}
          </ul>
        </>
      )}
      {v.observaciones.length > 0 && (
        <details className="mt-4">
          <summary className="cursor-pointer text-xs font-semibold text-slate-800">
            Review notes ({v.observaciones.length})
          </summary>
          <ul className="mt-1 list-disc space-y-1 pl-4 text-xs text-slate-700" lang="es">
            {v.observaciones.map((o, i) => (
              <li key={i}>{o}</li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function ChecklistCard({ items }: { items: string[] }) {
  return (
    <section aria-labelledby="checklist-heading" className={card}>
      <h2 id="checklist-heading" className={cardTitle}>
        Annex checklist
      </h2>
      <ul className="space-y-1.5 text-sm" lang="es">
        {items.map((item, i) => {
          const id = `annex-${i}`;
          return (
            <li key={i} className="flex gap-2">
              <input id={id} type="checkbox" className="mt-1 h-4 w-4 shrink-0" />
              <label htmlFor={id}>{item.replace(/^\d+\.\s*/, "")}</label>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function ChannelBox({ channel }: { channel: FilingChannel }) {
  const date = new Date(`${channel.consultado}T12:00:00Z`).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
  if (!channel.encontrado) {
    return (
      <div className="mb-4 rounded-md border border-slate-200 bg-slate-50 p-3 text-sm">
        <p className="text-xs font-semibold text-slate-800">Online filing in {channel.ciudad}</p>
        <p className="mt-1" lang="es">
          {channel.descripcion}
        </p>
        <p className="mt-1 text-xs text-slate-600">No specific official channel was found (searched {date}).</p>
      </div>
    );
  }
  return (
    <div className="mb-4 rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm">
      <p className="text-xs font-semibold text-emerald-900">Official channel for {channel.ciudad}</p>
      <p className="mt-1" lang="es">
        {channel.descripcion}
      </p>
      {(channel.enlace || channel.correo) && (
        <ul className="mt-1 space-y-0.5 text-sm">
          {channel.enlace && (
            <li>
              Link:{" "}
              <a href={channel.enlace} target="_blank" rel="noreferrer" className="break-all underline">
                {channel.enlace}
              </a>
            </li>
          )}
          {channel.correo && (
            <li>
              E-mail in the source:{" "}
              <a href={`mailto:${channel.correo}`} className="break-all underline">
                {channel.correo}
              </a>
            </li>
          )}
        </ul>
      )}
      {channel.fuente_url && (
        <p className="mt-2 text-xs text-slate-700">
          Source:{" "}
          <a href={channel.fuente_url} target="_blank" rel="noreferrer" className="break-all underline">
            {channel.fuente_titulo || host(channel.fuente_url)}
          </a>{" "}
          ({host(channel.fuente_url)}), consulted {date}.
        </p>
      )}
    </div>
  );
}

function FilingCard({ pkg }: { pkg: FinalPackage }) {
  return (
    <section aria-labelledby="filing-heading" className={card}>
      <h2 id="filing-heading" className={cardTitle}>
        Where to file
      </h2>
      {pkg.canal_radicacion && <ChannelBox channel={pkg.canal_radicacion} />}
      <p className="text-sm leading-relaxed" lang="es">
        {pkg.donde_radicar}
      </p>
      <h3 className="mb-1 mt-4 text-xs font-semibold text-slate-800">If the EPS does not comply</h3>
      <p className="text-sm leading-relaxed" lang="es">
        {pkg.que_hacer_si_no_cumplen}
      </p>
    </section>
  );
}

function RoutingPanel({ steps, totals }: { steps: Record<StepName, StepState>; totals: Totals | null }) {
  const real = totals?.cost_usd ?? 0;
  const ultra = totals?.cost_all_reasoning_usd ?? 0;
  const ratio = real > 0 && ultra > 0 ? ultra / real : null;
  return (
    <section aria-labelledby="routing-heading" className={card}>
      <h2 id="routing-heading" className={cardTitle}>
        Models and cost
      </h2>
      <dl className="grid grid-cols-3 gap-2 text-center">
        <div className="rounded-md bg-slate-50 p-2">
          <dt className="text-[11px] text-slate-600">Actual cost</dt>
          <dd className="text-base font-semibold">${real.toFixed(4)}</dd>
        </div>
        <div className="rounded-md bg-slate-50 p-2">
          <dt className="text-[11px] text-slate-600">All on Ultra</dt>
          <dd className="text-base font-semibold">${ultra.toFixed(4)}</dd>
        </div>
        <div className="rounded-md bg-emerald-50 p-2">
          <dt className="text-[11px] text-emerald-900">Routing saves</dt>
          <dd className="text-base font-semibold text-emerald-900">{ratio ? `${ratio.toFixed(1)}× cheaper` : "–"}</dd>
        </div>
      </dl>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full text-left text-xs">
          <caption className="sr-only">Model that resolved each step and its cost</caption>
          <thead className="text-slate-600">
            <tr>
              <th scope="col" className="py-1 pr-2 font-medium">Step</th>
              <th scope="col" className="py-1 pr-2 font-medium">Resolved by</th>
              <th scope="col" className="py-1 pr-2 text-right font-medium">Cost</th>
              <th scope="col" className="py-1 text-right font-medium">On Ultra</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {STEPS.map((name) => {
              const m = steps[name].metrics;
              if (!m) return null;
              return (
                <tr key={name} className="align-top">
                  <th scope="row" className="py-1.5 pr-2 font-medium">
                    {STEP_LABELS[name].title}
                  </th>
                  <td className="py-1.5 pr-2">
                    {modelLabel(m.final_model) || "no LLM"}
                    {m.escalations.map((e, k) => (
                      <span key={k} className="mt-0.5 block text-[11px] text-violet-800">
                        escalated from {modelLabel(e.from_model)}
                      </span>
                    ))}
                  </td>
                  <td className="py-1.5 pr-2 text-right tabular-nums">${m.cost_usd.toFixed(4)}</td>
                  <td className="py-1.5 text-right tabular-nums">${(m.cost_all_reasoning_usd ?? 0).toFixed(4)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-[11px] leading-snug text-slate-600">
        Escalations: {totals?.escalations ?? 0}. “All on Ultra” prices the same calls and tokens at the reasoning
        tier&apos;s rate; Ultra would also spend thinking tokens, so the real gap is larger. Prices are estimates.
      </p>
    </section>
  );
}

function SummaryCard({ summary }: { summary: ReviewerSummary | null }) {
  return (
    <section aria-labelledby="summary-heading" className={`${card} scroll-mt-4`}>
      <h2 id="summary-heading" className={cardTitle}>
        English summary for reviewers
      </h2>
      {!summary ? (
        <p className="text-sm text-slate-700">
          Not generated for this run. Turn on “English summary for reviewers” before drafting to get one.
        </p>
      ) : (
        <div className="space-y-3 text-sm leading-relaxed">
          <p>{summary.facts}</p>
          <div>
            <h3 className="text-xs font-semibold text-slate-800">Rights invoked</h3>
            <p>{summary.rights_invoked.join(", ")}</p>
          </div>
          <div>
            <h3 className="text-xs font-semibold text-slate-800">Requests to the judge</h3>
            <ul className="list-disc pl-4">
              {summary.requests.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          </div>
          {summary.rulings.length > 0 && (
            <div>
              <h3 className="text-xs font-semibold text-slate-800">Rulings cited and why</h3>
              <ul className="list-disc pl-4">
                {summary.rulings.map((r) => (
                  <li key={r.ruling_id}>
                    <span className="font-medium">{r.ruling_id}</span>: {r.why}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
