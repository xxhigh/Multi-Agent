import { AGENTS, TOOL_LABELS } from "@/lib/agents";
import type { Step } from "@/lib/run";

function formatArgs(args: Record<string, unknown>) {
  return Object.entries(args)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(", ");
}

function StepRow({ step }: { step: Step }) {
  if (step.kind === "route") {
    if (step.next === "FINISH") {
      return (
        <div className="text-sm text-muted">
          <span className={AGENTS.supervisor.text}>Supervisor</span> 작업 완료를 선언했어요
        </div>
      );
    }
    const target = AGENTS[step.next];
    return (
      <div className="text-sm">
        <span className={AGENTS.supervisor.text}>Supervisor</span>
        <span className="text-muted"> → </span>
        <span className={`font-medium ${target.text}`}>{target.label}</span>
        {step.instruction && <p className="mt-0.5 text-muted">{step.instruction}</p>}
        {step.reason && <p className="mt-0.5 text-xs text-muted">사유: {step.reason}</p>}
        {step.override && <p className="mt-0.5 text-xs text-amber-600 dark:text-amber-400">규칙 가드: {step.override}</p>}
      </div>
    );
  }

  if (step.kind === "tool") {
    const args = formatArgs(step.args);
    return (
      <details className="group text-sm">
        <summary className="cursor-pointer list-none">
          <span className="mr-1.5 inline-block text-muted transition group-open:rotate-90">›</span>
          <span className={AGENTS[step.agent].text}>{AGENTS[step.agent].label}</span>
          <span className="text-muted"> 도구 </span>
          <span className="font-medium">{TOOL_LABELS[step.name] ?? step.name}</span>
          {args && <code className="ml-1.5 font-mono text-xs text-muted">({args})</code>}
          {!step.preview && <span className="ml-2 text-xs text-muted animate-pulse">실행 중…</span>}
        </summary>
        {step.preview && (
          <pre className="mt-1.5 ml-4 max-h-48 overflow-auto rounded-md bg-subtle p-2.5 font-mono text-xs whitespace-pre-wrap text-muted">
            {step.preview}
            {step.preview.length >= 300 && " …"}
          </pre>
        )}
      </details>
    );
  }

  if (step.kind === "eval") {
    const scores = Object.entries(step.scores)
      .map(([k, v]) => `${k} ${v}/5`)
      .join(" · ");
    return (
      <details className="group text-sm" open={!step.passed}>
        <summary className="cursor-pointer list-none">
          <span className="mr-1.5 inline-block text-muted transition group-open:rotate-90">›</span>
          <span className={`font-medium ${AGENTS.evaluator.text}`}>Evaluator</span>{" "}
          {step.passed ? "품질 평가 통과" : "품질 평가 미달 → 재작업"}
          {scores && <span className="text-muted"> · {scores}</span>}
        </summary>
        {step.issues.length > 0 && (
          <ul className="mt-1.5 ml-4 list-disc space-y-0.5 pl-4 text-xs text-muted">
            {step.issues.map((issue, i) => (
              <li key={i}>{issue}</li>
            ))}
          </ul>
        )}
      </details>
    );
  }

  const meta = AGENTS[step.agent];
  return (
    <details className="group text-sm">
      <summary className="cursor-pointer list-none">
        <span className="mr-1.5 inline-block text-muted transition group-open:rotate-90">›</span>
        <span className={`font-medium ${meta.text}`}>{meta.label}</span> {step.failed ? "작업 실패" : "작업 완료"}
        {step.attempt && step.attempt > 1 && <span className="text-muted"> · 재작업 {step.attempt - 1}회차</span>}
        <span className="text-muted"> · {step.chars.toLocaleString()}자</span>
      </summary>
      {step.summary && (
        <div className="mt-1.5 ml-4 max-h-64 overflow-auto rounded-md bg-subtle p-2.5 text-xs whitespace-pre-wrap text-muted">
          {step.summary}
        </div>
      )}
    </details>
  );
}

export function Timeline({ steps }: { steps: Step[] }) {
  return (
    <ol className="space-y-2.5 border-l border-line pl-4">
      {steps.map((step, i) => {
        const agent = step.kind === "route" ? "supervisor" : step.agent;
        return (
          <li key={i} className="relative">
            <span className={`absolute top-1.5 -left-[21px] size-2 rounded-full ${AGENTS[agent].dot}`} />
            <StepRow step={step} />
          </li>
        );
      })}
    </ol>
  );
}
