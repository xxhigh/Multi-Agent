"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { RunCard } from "@/components/RunCard";
import { ReportView } from "@/components/ReportView";
import { Sidebar } from "@/components/Sidebar";
import { getReport, listReports, streamAnalyze, type ReportMeta } from "@/lib/api";
import { applyEvent, newRun, type Run } from "@/lib/run";

const EXAMPLES = ["삼성전자 재무 분석해줘", "SK하이닉스와 삼성전자 수익성 비교", "네이버 최근 공시 요약", "현대차 작년 매출은?"];

export default function Home() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [reports, setReports] = useState<ReportMeta[]>([]);
  const [reportsError, setReportsError] = useState<string | null>(null);
  const [viewing, setViewing] = useState<{ name: string; content: string } | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const running = runs.some((r) => r.status === "running");

  const refreshReports = useCallback(async () => {
    try {
      setReports(await listReports());
      setReportsError(null);
    } catch {
      setReportsError("백엔드에 연결할 수 없어요. API 서버가 켜져 있는지 확인하세요.");
    }
  }, []);

  useEffect(() => {
    refreshReports();
  }, [refreshReports]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [runs.length]);

  const update = (id: string, fn: (r: Run) => Run) => setRuns((rs) => rs.map((r) => (r.id === id ? fn(r) : r)));

  const ask = async (question: string) => {
    const q = question.trim();
    if (!q || running) return;
    const run = newRun(q);
    setRuns((rs) => [...rs, run]);
    setInput("");
    setViewing(null);

    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await streamAnalyze(
        q,
        threadId,
        (e) => {
          if (e.type === "start") setThreadId(e.thread_id);
          update(run.id, (r) => applyEvent(r, e));
          if (e.type === "report") refreshReports();
        },
        controller.signal,
      );
      // done 이벤트 없이 스트림이 끝난 경우 (서버 중단 등)
      update(run.id, (r) =>
        r.status === "running"
          ? { ...r, status: "error", error: "서버 연결이 끊겼어요.", active: null, endedAt: Date.now() }
          : r,
      );
    } catch (err) {
      const stopped = controller.signal.aborted;
      update(run.id, (r) => ({
        ...r,
        status: stopped ? "stopped" : "error",
        error: stopped ? undefined : `요청 실패: ${(err as Error).message}`,
        active: null,
        endedAt: Date.now(),
      }));
    } finally {
      abortRef.current = null;
    }
  };

  const reset = () => {
    abortRef.current?.abort();
    setRuns([]);
    setThreadId(null);
    setViewing(null);
    setMenuOpen(false);
  };

  const openReport = async (name: string) => {
    setMenuOpen(false);
    try {
      setViewing({ name, content: await getReport(name) });
    } catch {
      setReportsError("리포트를 불러오지 못했어요.");
    }
  };

  return (
    <div className="flex h-dvh">
      {/* 사이드바: 데스크톱 고정, 모바일은 오버레이 */}
      <aside
        className={`fixed inset-y-0 left-0 z-30 w-72 border-r border-line bg-sidebar transition-transform md:static md:translate-x-0 ${
          menuOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <Sidebar
          reports={reports}
          selected={viewing?.name ?? null}
          onSelect={openReport}
          onNew={reset}
          error={reportsError}
        />
      </aside>
      {menuOpen && (
        <button aria-label="메뉴 닫기" className="fixed inset-0 z-20 bg-black/30 md:hidden" onClick={() => setMenuOpen(false)} />
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-line px-4 py-3">
          <button className="btn-ghost md:hidden" onClick={() => setMenuOpen(true)} aria-label="메뉴 열기">
            ☰
          </button>
          <h1 className="font-semibold tracking-tight">재무 분석 에이전트</h1>
          <span className="hidden text-xs text-muted sm:inline">DART 공시 · 사업보고서 RAG · 멀티 에이전트</span>
        </header>

        <main className="flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-3xl space-y-10 px-4 py-8">
            {viewing ? (
              <div className="space-y-3">
                <button onClick={() => setViewing(null)} className="btn-ghost">
                  ← {runs.length ? "대화로 돌아가기" : "처음으로"}
                </button>
                <ReportView content={viewing.content} name={viewing.name} />
              </div>
            ) : runs.length === 0 ? (
              <div className="pt-[12vh] text-center">
                <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">어떤 기업을 분석할까요?</h2>
                <p className="mt-3 text-sm text-muted">
                  국내 상장사 이름을 입력하면 Researcher · Analyst · Writer 에이전트가 협업해 리포트를 작성해요.
                </p>
                <div className="mt-8 flex flex-wrap justify-center gap-2">
                  {EXAMPLES.map((ex) => (
                    <button key={ex} onClick={() => ask(ex)} className="rounded-full border border-line px-3.5 py-1.5 text-sm transition hover:bg-subtle">
                      {ex}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              runs.map((run) => <RunCard key={run.id} run={run} />)
            )}
            <div ref={bottomRef} />
          </div>
        </main>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            ask(input);
          }}
          className="border-t border-line bg-background px-4 py-3"
        >
          <div className="mx-auto flex max-w-3xl gap-2">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={runs.length ? "후속 질문을 입력하세요 (예: 배당은 어때?)" : "예: 카카오 재무 분석해줘"}
              maxLength={500}
              className="min-w-0 flex-1 rounded-xl border border-line bg-surface px-4 py-2.5 outline-none focus:ring-2 focus:ring-accent/40"
            />
            {running ? (
              <button type="button" onClick={() => abortRef.current?.abort()} className="btn-secondary">
                중단
              </button>
            ) : (
              <button type="submit" disabled={!input.trim()} className="btn-primary">
                분석
              </button>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}
