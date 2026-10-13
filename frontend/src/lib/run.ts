import type { Agent, AgentEvent } from "./api";

export type Step =
  | { kind: "route"; next: Agent | "FINISH"; instruction: string }
  | { kind: "tool"; agent: Agent; id: string; name: string; args: Record<string, unknown>; preview?: string }
  | { kind: "done"; agent: Agent; chars: number; summary: string };

export type Run = {
  id: string;
  question: string;
  status: "running" | "done" | "error" | "stopped";
  steps: Step[];
  active: Agent | null;
  completed: Agent[];
  report?: { content: string; name: string };
  error?: string;
  startedAt: number;
  endedAt?: number;
};

export function newRun(question: string): Run {
  return {
    id: crypto.randomUUID(),
    question,
    status: "running",
    steps: [],
    active: "supervisor",
    completed: [],
    startedAt: Date.now(),
  };
}

/** SSE 이벤트 하나를 실행 상태에 반영한다. */
export function applyEvent(run: Run, e: AgentEvent): Run {
  switch (e.type) {
    case "route":
      return {
        ...run,
        active: e.next === "FINISH" ? null : e.next,
        steps: [...run.steps, { kind: "route", next: e.next, instruction: e.instruction }],
      };
    case "tool_call":
      return { ...run, active: e.agent, steps: [...run.steps, { kind: "tool", ...e }] };
    case "tool_result":
      return {
        ...run,
        steps: run.steps.map((s) => (s.kind === "tool" && s.id === e.id ? { ...s, preview: e.preview } : s)),
      };
    case "worker_done":
      return {
        ...run,
        active: "supervisor",
        completed: run.completed.includes(e.agent) ? run.completed : [...run.completed, e.agent],
        steps: [...run.steps, { kind: "done", agent: e.agent, chars: e.chars, summary: e.summary }],
      };
    case "report":
      return { ...run, report: { content: e.content, name: e.name } };
    case "error":
      return { ...run, status: "error", error: e.message };
    case "done":
      return { ...run, active: null, status: run.status === "running" ? "done" : run.status, endedAt: Date.now() };
    default:
      return run;
  }
}
