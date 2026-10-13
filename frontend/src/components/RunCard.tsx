"use client";

import { useEffect, useState } from "react";
import type { Run } from "@/lib/run";
import { Pipeline } from "./Pipeline";
import { ReportView } from "./ReportView";
import { Timeline } from "./Timeline";

function Elapsed({ run }: { run: Run }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (run.endedAt) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [run.endedAt]);
  const sec = Math.max(0, Math.round(((run.endedAt ?? now) - run.startedAt) / 1000));
  return <span className="tabular-nums">{sec >= 60 ? `${Math.floor(sec / 60)}분 ${sec % 60}초` : `${sec}초`}</span>;
}

const STATUS = {
  running: "분석 중",
  done: "완료",
  error: "오류",
  stopped: "중단됨",
};

export function RunCard({ run }: { run: Run }) {
  const running = run.status === "running";
  const toolCount = run.steps.filter((s) => s.kind === "tool").length;

  return (
    <section className="space-y-4">
      <div className="flex justify-end">
        <p className="max-w-[85%] rounded-2xl rounded-br-md bg-accent px-4 py-2.5 text-accent-fg">{run.question}</p>
      </div>

      <div className="rounded-xl border border-line bg-surface p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
          <span className="flex items-center gap-2">
            {running && <span className="size-2 animate-pulse rounded-full bg-accent" />}
            <span className="font-medium text-foreground">{STATUS[run.status]}</span>
            <span>· 도구 {toolCount}회</span>
            <span>
              · <Elapsed run={run} />
            </span>
          </span>
        </div>
        <Pipeline active={run.active} completed={run.completed} />

        {run.steps.length > 0 && (
          <details open={running} className="group mt-4">
            <summary className="cursor-pointer list-none text-xs font-medium text-muted hover:text-foreground">
              <span className="mr-1 inline-block transition group-open:rotate-90">›</span>
              진행 과정 {running ? "" : `(${run.steps.length}단계)`}
            </summary>
            <div className="mt-3">
              <Timeline steps={run.steps} />
            </div>
          </details>
        )}
      </div>

      {run.error && (
        <div className="rounded-xl border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          {run.error}
        </div>
      )}

      {run.report && <ReportView content={run.report.content} name={run.report.name} />}
    </section>
  );
}
