import type { Agent } from "./api";

export const AGENTS: Record<Agent, { label: string; role: string; dot: string; text: string; ring: string }> = {
  supervisor: { label: "Supervisor", role: "작업 배분", dot: "bg-slate-500", text: "text-slate-600 dark:text-slate-300", ring: "ring-slate-400" },
  researcher: { label: "Researcher", role: "데이터 수집", dot: "bg-sky-500", text: "text-sky-700 dark:text-sky-300", ring: "ring-sky-400" },
  analyst: { label: "Analyst", role: "비율 분석", dot: "bg-violet-500", text: "text-violet-700 dark:text-violet-300", ring: "ring-violet-400" },
  writer: { label: "Writer", role: "리포트 작성", dot: "bg-emerald-500", text: "text-emerald-700 dark:text-emerald-300", ring: "ring-emerald-400" },
};

export const TOOL_LABELS: Record<string, string> = {
  search_company: "기업 검색",
  get_company_overview: "기업 개황",
  get_financial_statements: "재무제표",
  get_price_summary: "주가 요약",
  get_recent_disclosures: "최근 공시",
  search_business_report: "사업보고서 검색",
  calculate_financial_ratios: "재무 비율 계산",
  calculator: "계산기",
};
