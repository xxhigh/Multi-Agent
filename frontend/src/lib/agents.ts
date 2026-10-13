import type { Agent } from "./api";

export const AGENTS: Record<Agent, { label: string; role: string; dot: string; text: string; ring: string }> = {
  supervisor: { label: "Supervisor", role: "작업 배분", dot: "bg-slate-500", text: "text-slate-600 dark:text-slate-300", ring: "ring-slate-400" },
  researcher: { label: "Researcher", role: "회사·기술", dot: "bg-sky-500", text: "text-sky-700 dark:text-sky-300", ring: "ring-sky-400" },
  market_researcher: { label: "Market", role: "시장 조사", dot: "bg-teal-500", text: "text-teal-700 dark:text-teal-300", ring: "ring-teal-400" },
  investment_analyst: { label: "Investment", role: "재무·투자조건", dot: "bg-violet-500", text: "text-violet-700 dark:text-violet-300", ring: "ring-violet-400" },
  writer: { label: "Writer", role: "리포트 작성", dot: "bg-emerald-500", text: "text-emerald-700 dark:text-emerald-300", ring: "ring-emerald-400" },
  evaluator: { label: "Evaluator", role: "품질 평가", dot: "bg-amber-500", text: "text-amber-700 dark:text-amber-300", ring: "ring-amber-400" },
};

export const TOOL_LABELS: Record<string, string> = {
  search_company: "기업 검색",
  get_company_overview: "기업 개황",
  get_recent_disclosures: "DART 공시",
  search_company_docs: "기업 자료 검색",
  search_industry_docs: "산업 리포트 검색",
  get_audit_financials: "감사보고서 재무",
  search_audit_report: "감사보고서 주석 검색",
  calculator: "계산기",
};
