"""리포트 품질 평가 노드 (Hybrid = 규칙 검사 + LLM Judge).

| 평가 항목      | 규칙 검사 (결정적, 형식)                                   | LLM Judge (내용)                          |
|---------------|----------------------------------------------------------|-------------------------------------------|
| Groundedness  | SUMMARY/REFERENCE 목차, [S#] 인용이 출처 목록에 존재,        | 주장·해석이 근거 자료로 뒷받침되는가          |
|               | 리포트의 금액·비율 수치가 근거 자료에 그대로 존재             |                                           |
| 중립성         | 투자 권유 표현(매수/매도 의견, 목표주가, 비중확대 등) 금지     | 근거 없는 우열 판정·단정적 전망이 없는가      |
| 편향 통제      | 출처 제공처 2종 이상(DART·Yahoo), 출처 3개 이상 인용,          | 강점과 리스크가 균형 있게 다뤄졌는가          |
|               | 리스크 섹션 존재                                           |                                           |

둘 다 통과해야 pass. 미달이면 graph.py의 조건부 엣지가 supervisor로 되돌려 재작성/재수집 Loop를 돈다.
"""

import os
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from .agents import llm
from .observability import log_decision
from .prompts import JUDGE_PROMPT
from .state import MAX_REVISIONS, EvalResult, FinanceState, JudgeVerdict

_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?\s?(?:조|억|만원|원|%|배)")
_CITE = re.compile(r"\[(S\d+)\]")
_RECOMMEND = [
    r"매수\s*(?:의견|추천|권고|권장|하세요|할\s*만)", r"매도\s*(?:의견|추천|권고|권장|하세요)",
    r"목표\s*주가", r"비중\s*(?:확대|축소)", r"(?:투자|매수|매입)를?\s*(?:추천|권장|권합)",
    r"(?:사야|팔아야)\s*(?:한다|합니다)", r"(?:저평가|고평가)\s*(?:매력|기회)", r"강력\s*추천",
]
GROUNDED_MIN_RATIO = 0.9


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _section(report: str, name: str) -> str | None:
    m = re.search(rf"^##\s*{name}\b.*?$(.*?)(?=^##\s|\Z)", report, re.S | re.M | re.I)
    return m.group(1).strip() if m else None


def rule_checks(report: str, state: FinanceState) -> tuple[dict[str, bool], list[str]]:
    task = state.get("task") or {}
    sources = {s["id"]: s for s in state.get("sources") or []}
    evidence = _norm("\n".join(f["content"] for f in (state.get("findings") or {}).values()))
    is_analysis = task.get("task_type") == "analysis"
    checks, issues = {}, []

    # ── Groundedness ──
    summary, reference = _section(report, "SUMMARY"), _section(report, "REFERENCE")
    checks["has_summary"] = bool(summary)
    checks["has_reference"] = bool(reference)
    if not summary:
        issues.append("'## SUMMARY' 섹션이 없습니다.")
    if not reference:
        issues.append("'## REFERENCE' 섹션이 없습니다.")

    body = report.split("## REFERENCE")[0]
    cited = list(dict.fromkeys(_CITE.findall(body)))
    unknown = [c for c in cited if c not in sources]
    checks["citations_valid"] = bool(cited) and not unknown
    if not cited:
        issues.append("본문에 [S#] 형식의 출처 인용이 없습니다.")
    if unknown:
        issues.append(f"출처 목록에 없는 인용: {', '.join(unknown)}")

    numbers = list(dict.fromkeys(_norm(n) for n in _NUMBER.findall(body)))
    ungrounded = [n for n in numbers if n not in evidence]
    ratio = 1 - len(ungrounded) / len(numbers) if numbers else 1.0
    checks["numbers_grounded"] = ratio >= GROUNDED_MIN_RATIO
    if not checks["numbers_grounded"]:
        issues.append(f"근거 자료에 없는 수치({len(ungrounded)}/{len(numbers)}개): {', '.join(ungrounded[:8])} "
                      "— 근거에 있는 표기 그대로 인용하거나 삭제하세요.")

    # ── 중립성 ──
    hits = [m.group(0) for p in _RECOMMEND for m in re.finditer(p, body)]
    checks["no_recommendation"] = not hits
    if hits:
        issues.append(f"투자 권유로 읽히는 표현: {', '.join(dict.fromkeys(hits))}")

    # ── 편향 통제 ──
    if task.get("task_type") != "out_of_scope":
        providers = {p for c in cited if c in sources for p in sources[c]["provider"].split("·")}
        need_providers, need_sources = (2, 3) if is_analysis else (1, 1)
        checks["multi_provider"] = len(providers) >= need_providers
        checks["multi_source"] = len([c for c in cited if c in sources]) >= need_sources
        if not checks["multi_provider"]:
            issues.append(f"인용 출처 제공처가 {len(providers)}종뿐입니다 (DART·Yahoo Finance 등 {need_providers}종 이상 필요).")
        if not checks["multi_source"]:
            issues.append(f"서로 다른 출처를 {need_sources}개 이상 인용하세요.")
        if is_analysis:
            checks["has_risk_section"] = bool(_section(report, r"[^\n]*리스크"))
            if not checks["has_risk_section"]:
                issues.append("강점과 함께 리스크를 다루는 섹션이 없습니다.")
    return checks, issues


def evaluator_node(state: FinanceState) -> dict:
    trace_id, report = state["trace_id"], state.get("report", "")
    task = state.get("task") or {}

    if task.get("task_type") == "out_of_scope":
        result = EvalResult(passed=True, rule_checks={}, rule_issues=[], judge=None,
                            fix_target="writer", feedback="지원 범위 밖 안내 — 평가 생략")
        log_decision(trace_id, "evaluator", "verdict", passed=True, skipped=True)
        return {"eval_result": result}

    checks, issues = rule_checks(report, state)
    evidence = "\n\n".join(f"=== {a} ===\n{f['content']}" for a, f in (state.get("findings") or {}).items())
    sources = "\n".join(f"[{s['id']}] {s['label']} ({s['provider']})" for s in state.get("sources") or [])
    verdict: JudgeVerdict = llm("JUDGE_MODEL").with_structured_output(JudgeVerdict).invoke(
        [SystemMessage(JUDGE_PROMPT),
         HumanMessage(f"사용자 질문: {state['question']}\n\n[근거 자료]\n{evidence}\n\n[출처 목록]\n{sources}"
                      f"\n\n[평가 대상 리포트]\n{report}")],
        {"run_name": "evaluator.judge", "metadata": {"trace_id": trace_id}})
    judge_ok = verdict.groundedness.passed and verdict.neutrality.passed and verdict.bias_control.passed
    rules_ok = all(checks.values())

    fix_target = verdict.fix_target
    if not rules_ok and judge_ok:
        fix_target = "writer"  # 형식·인용 문제는 writer가 고친다
    judge_issues = [f"[{k}] {i}" for k in ("groundedness", "neutrality", "bias_control")
                    for i in getattr(verdict, k).issues]
    result = EvalResult(passed=rules_ok and judge_ok, rule_checks=checks, rule_issues=issues + judge_issues,
                        judge=verdict.model_dump(), fix_target=fix_target, feedback=verdict.feedback)
    log_decision(trace_id, "evaluator", "verdict", passed=result["passed"], rule_checks=checks,
                 issues=result["rule_issues"],
                 judge={k: getattr(verdict, k).score for k in ("groundedness", "neutrality", "bias_control")},
                 fix_target=fix_target, revision=state.get("revision_count", 0),
                 judge_model=os.getenv("JUDGE_MODEL") or os.getenv("OPENAI_MODEL", "gpt-5-mini"))
    return {"eval_result": result}


def route_after_eval(state: FinanceState) -> str:
    """통과 → finalize / 미달 + 수정 여력 → supervisor (Loop) / 상한 도달 → finalize(미달 표시)."""
    ev = state["eval_result"]
    if ev["passed"] or state.get("revision_count", 0) >= MAX_REVISIONS:
        return "finalize"
    return "supervisor"


def finalize_node(state: FinanceState) -> dict:
    """최종 리포트를 대화 기록에 남긴다 (후속 질문 컨텍스트). 미달로 끝났으면 경고를 덧붙인다."""
    report = state.get("report") or "리포트를 생성하지 못했습니다."
    ev = state.get("eval_result")
    if ev and not ev["passed"]:
        report += ("\n\n> ⚠️ 품질 평가 기준을 모두 충족하지 못한 상태로 수정 한도에 도달했습니다: "
                   + "; ".join(ev["rule_issues"][:3]))
    log_decision(state["trace_id"], "finalize", "end", passed=bool(ev and ev["passed"]),
                 steps=state.get("step_count"), revisions=state.get("revision_count", 0),
                 rework=state.get("rework_count"), sources=len(state.get("sources") or []))
    return {"report": report, "messages": [AIMessage(content=report, name="writer")]}
