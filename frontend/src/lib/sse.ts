import type { CaseEvent } from "./types";

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

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
