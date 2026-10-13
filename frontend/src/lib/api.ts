export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Agent = "supervisor" | "researcher" | "analyst" | "writer" | "evaluator";

export type AgentEvent =
  | { type: "start"; thread_id: string }
  | { type: "route"; next: Agent | "FINISH"; instruction: string }
  | { type: "tool_call"; agent: Agent; id: string; name: string; args: Record<string, unknown> }
  | { type: "tool_result"; agent: Agent; id: string; preview: string }
  | { type: "worker_done"; agent: Agent; chars: number; summary: string }
  | { type: "report"; content: string; name: string }
  | { type: "error"; message: string }
  | { type: "done" };

export type ReportMeta = { name: string; title: string; created_at: string };

/** POST /api/analyze 의 SSE 응답을 이벤트 단위로 넘겨준다. (EventSource는 POST를 못 써서 직접 파싱) */
export async function streamAnalyze(
  question: string,
  threadId: string | null,
  onEvent: (e: AgentEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_URL}/api/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, thread_id: threadId }),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`서버 오류 (${res.status})`);

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const raw = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      let event = "message";
      let data = "";
      for (const line of raw.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      onEvent({ type: event, ...JSON.parse(data || "{}") } as AgentEvent);
    }
  }
}

export async function listReports(): Promise<ReportMeta[]> {
  const res = await fetch(`${API_URL}/api/reports`);
  if (!res.ok) throw new Error(`서버 오류 (${res.status})`);
  return res.json();
}

export async function getReport(name: string): Promise<string> {
  const res = await fetch(`${API_URL}/api/reports/${encodeURIComponent(name)}`);
  if (!res.ok) throw new Error(`서버 오류 (${res.status})`);
  return (await res.json()).content;
}
