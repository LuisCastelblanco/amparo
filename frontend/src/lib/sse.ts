import type { CaseEvent, Refusal, SampleRun, TutelaDraft } from "./types";

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

/** The API refused to start a live run (a usage limit, the kill switch or a busy instance). */
export class RunRefusedError extends Error {
  constructor(public refusal: Refusal) {
    super(refusal.message);
    this.name = "RunRefusedError";
  }
}

export interface ClarifyingAnswer {
  question: string;
  answer: string;
}

/** Throw a RunRefusedError for a limit refusal, or a plain Error otherwise. */
async function throwApiError(response: Response): Promise<never> {
  const detail = await response.text().catch(() => "");
  let refusal: Refusal | null = null;
  try {
    const body = JSON.parse(detail);
    if (typeof body?.error === "string" && typeof body?.message === "string") refusal = body;
  } catch {
    // not a JSON refusal
  }
  if (refusal && refusal.error !== "intake_failed") throw new RunRefusedError(refusal);
  throw new Error(`API error ${response.status}: ${detail.slice(0, 300)}`);
}

/** Intake only: questions for critical missing information (may be empty). */
export async function checkIntake(narrative: string, signal?: AbortSignal): Promise<{ questions: string[] }> {
  const response = await fetch(`${API_URL}/api/cases/intake`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ narrative }),
    signal,
  });
  if (!response.ok) await throwApiError(response);
  return response.json();
}

/** POST a narrative and yield each Server-Sent Event from the pipeline. */
export async function* runCase(
  narrative: string,
  options: { englishSummary?: boolean; answers?: ClarifyingAnswer[] } = {},
  signal?: AbortSignal,
): AsyncGenerator<CaseEvent> {
  const response = await fetch(`${API_URL}/api/cases/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      narrative,
      english_summary: options.englishSummary ?? false,
      answers: options.answers ?? [],
    }),
    signal,
  });
  if (!response.ok || !response.body) await throwApiError(response);
  const body = response.body!;

  const reader = body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value.replace(/\r\n/g, "\n");
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      let event = "message";
      const data: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      if (data.length) yield { event, data: JSON.parse(data.join("\n")) } as CaseEvent;
    }
  }
}

const sleep = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    });
  });

/** Replay a recorded run with its original pacing, sped up and with long waits shortened. */
export async function* replaySample(sample: SampleRun, signal?: AbortSignal, speed = 3): AsyncGenerator<CaseEvent> {
  let previous = 0;
  for (const { t, ...event } of sample.events) {
    await sleep(Math.min(((t - previous) * 1000) / speed, 2500), signal);
    previous = t;
    yield event as CaseEvent;
  }
}

export interface UsageStatus {
  live_runs_enabled: boolean;
}

export async function fetchUsageStatus(signal?: AbortSignal): Promise<UsageStatus | null> {
  try {
    const response = await fetch(`${API_URL}/api/usage`, { signal });
    return response.ok ? await response.json() : null;
  } catch {
    return null;
  }
}

/** Generate the tutela as .docx or PDF on the API (no LLM involved) and save it. */
export async function downloadDocument(draft: TutelaDraft, format: "docx" | "pdf"): Promise<void> {
  const response = await fetch(`${API_URL}/api/documents/tutela.${format}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(draft),
  });
  if (!response.ok) throw new Error(`Could not generate the ${format.toUpperCase()} file (error ${response.status}).`);
  const filename =
    response.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1] ?? `tutela.${format}`;
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}
