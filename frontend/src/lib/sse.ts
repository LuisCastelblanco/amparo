import type { CaseEvent, Refusal, SampleRun } from "./types";

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

/** The API refused to start a live run (a usage limit, the kill switch or a busy instance). */
export class RunRefusedError extends Error {
  constructor(public refusal: Refusal) {
    super(refusal.message);
    this.name = "RunRefusedError";
  }
}

/** POST a narrative and yield each Server-Sent Event from the pipeline. */
export async function* runCase(narrative: string, signal?: AbortSignal): AsyncGenerator<CaseEvent> {
  const response = await fetch(`${API_URL}/api/cases/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ narrative }),
    signal,
  });
  if (!response.ok || !response.body) {
    const detail = await response.text().catch(() => "");
    let refusal: Refusal | null = null;
    try {
      const body = JSON.parse(detail);
      if (typeof body?.error === "string" && typeof body?.message === "string") refusal = body;
    } catch {
      // not a JSON refusal
    }
    if (refusal) throw new RunRefusedError(refusal);
    throw new Error(`API error ${response.status}: ${detail.slice(0, 300)}`);
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
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
