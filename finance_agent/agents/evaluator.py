"""보고서 품질 평가 노드 (노션 평가 방식 3안: Hybrid = 규칙 검사 + LLM Judge).

순서: 결정론적 규칙 검사를 먼저 하고, 통과한 보고서만 LLM Judge로 보낸다 (비용·재현성).
평가 '판정'은 확률적일 수 있지만, 판정 결과에 따른 '다음 경로'는 supervisor가 코드로 정한다.
"""

import re

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from .. import config, llm
from ..observability import load_evidence, log_decision
from ..prompts import JUDGE_PROMPT
from ..state import CriterionResult, EvalVerdict, EvidenceRef, FinanceState, PerspectiveKey
from ..tools import COLLECTOR_BY_TYPE
from .writer import CITATION, DISCLAIMER

# 중립성: 우열 판정·투자 권유 표현 (면책 문구 줄은 검사에서 제외)
BANNED = ("매수", "매도", "추천", "유망", "저평가", "고평가", "목표주가", "투자 권유", "투자를 권",
          "투자해야", "투자하지 말", "투자 적격", "투자 부적격", "보다 우위", "보다 우월", "열위")
SECTION_TITLES = {
    "SUMMARY": "SUMMARY",
    **{k: v["title"] for k, v in config.PERSPECTIVES.items()},
    "REFERENCE": "REFERENCE",
}
MIN_SECTION_CHARS = 80
_JUDGE_EVIDENCE_CHARS = 14000


class JudgeResult(BaseModel):
    groundedness: CriterionResult
    neutrality: CriterionResult
    bias_control: CriterionResult
    coverage: CriterionResult
    evidence_gaps: list[PerspectiveKey] = Field(default_factory=list)
    feedback: str = ""


def split_sections(report: str) -> dict[str, str]:
    """'## 제목' 기준으로 절을 나눈다."""
    sections, current = {}, None
    for line in report.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            current = m.group(1).strip()
            sections[current] = ""
        elif current:
            sections[current] += line + "\n"
    return sections


def _find_section(sections: dict[str, str], title: str) -> str | None:
    for name, body in sections.items():
        if name.startswith(title):
            return body
    return None


def pdf_pages(report: str) -> int | None:
    """제출용 PDF로 렌더링했을 때의 쪽수 (제출 조건: 최대 10장). 변환기가 없으면 None."""
    try:
        import tempfile
        from pathlib import Path

        import pymupdf

        from ..pdf_export import markdown_to_pdf
        with tempfile.TemporaryDirectory() as d:
            path = markdown_to_pdf(report, Path(d) / "check.pdf")
            with pymupdf.open(str(path)) as doc:
                return doc.page_count
    except Exception:  # noqa: BLE001
        return None


def rule_check(report: str, evidence: list[EvidenceRef], perspectives: dict) -> dict[str, CriterionResult]:
    by_id = {e.id: e for e in evidence}
    sections = split_sections(report)
    cited_all = [f"E{n}" for n in CITATION.findall(report)]

    # 관점 커버리지: 필수 목차와 최소 분량
    cov = []
    for key, title in SECTION_TITLES.items():
        body = _find_section(sections, title)
        if body is None:
            cov.append(f"필수 절 '{title}' 없음")
        elif key not in ("REFERENCE",) and len(body.strip()) < MIN_SECTION_CHARS:
            cov.append(f"'{title}' 내용이 너무 짧음({len(body.strip())}자)")
    pages = pdf_pages(report) if not cov else None
    if pages is not None and pages > config.MAX_PDF_PAGES:
        cov.append(f"PDF {pages}장 — 제출 상한 {config.MAX_PDF_PAGES}장 초과, 본문을 줄일 것")

    # 근거성: 인용 존재·유효성, 관점 절마다 인용
    gr = []
    unknown = sorted({c for c in cited_all if c not in by_id})
    if unknown:
        gr.append(f"존재하지 않는 근거 ID 인용: {', '.join(unknown)}")
    if not cited_all:
        gr.append("본문에 근거 인용([E#])이 하나도 없음")

    # 편향 통제: 관점 절마다 서로 다른 근거 2개 이상, 보고서 전체 출처 3종 이상
    bias = []
    for key in config.PERSPECTIVE_KEYS:
        body = _find_section(sections, SECTION_TITLES[key]) or ""
        ids = {f"E{n}" for n in CITATION.findall(body)} & set(by_id)
        p = perspectives.get(key)
        limited = bool(p and p.limited)
        if not ids and not limited:
            gr.append(f"'{SECTION_TITLES[key]}' 절에 근거 인용 없음")
        available = len(set(p.evidence_ids)) if p else 0
        if len(ids) < 2 and available >= 2 and not limited:
            bias.append(f"'{SECTION_TITLES[key]}' 절이 근거 {len(ids)}개에만 의존 (사용 가능 {available}개)")
    providers_available = {e.provider for e in evidence}
    providers_cited = {by_id[c].provider for c in cited_all if c in by_id}
    if len(providers_available) >= 3 and len(providers_cited) < 3:
        bias.append(f"보고서 전체가 출처 {len(providers_cited)}종에만 의존 (사용 가능 {len(providers_available)}종)")

    # 중립성: 금지 표현
    neu = []
    for line in report.splitlines():
        if DISCLAIMER in line:
            continue
        hits = [w for w in BANNED if w in line]
        if hits:
            neu.append(f"금지 표현 {hits}: \"{line.strip()[:60]}\"")

    return {
        "groundedness": CriterionResult(passed=not gr, issues=gr),
        "neutrality": CriterionResult(passed=not neu, issues=neu),
        "bias_control": CriterionResult(passed=not bias, issues=bias),
        "coverage": CriterionResult(passed=not cov, issues=cov),
    }


def _feedback(results: dict[str, CriterionResult]) -> str:
    return "\n".join(f"- [{name}] {issue}" for name, r in results.items() for issue in r.issues)


def evaluator_node(state: FinanceState) -> dict:
    trace_id = state["trace_id"]
    report = state.get("report") or ""
    evidence = state.get("evidence") or []
    perspectives = state.get("perspectives") or {}

    rules = rule_check(report, evidence, perspectives)
    if not all(r.passed for r in rules.values()):
        verdict = EvalVerdict(passed=False, judge_used=False, fix_target="writer",
                              feedback=_feedback(rules), **rules)
        log_decision(trace_id, "evaluator", "FAIL", "규칙 검사 미달 — LLM Judge 생략",
                     issues=_feedback(rules).splitlines()[:8])
        return {"eval_result": verdict, "node_status": {"evaluator": "ok"}}

    excerpts, total = [], 0
    for e in evidence:
        chunk = f"[{e.id}] ({e.provider}) {load_evidence(e.path, 1500)}"
        if total + len(chunk) > _JUDGE_EVIDENCE_CHARS:
            break
        excerpts.append(chunk)
        total += len(chunk)
    try:
        judge: JudgeResult = llm.structured(
            JudgeResult,
            [SystemMessage(JUDGE_PROMPT),
             HumanMessage(f"## 근거 원문\n{chr(10).join(excerpts)}\n\n## 평가 대상 보고서\n{report}")],
            run_name="quality_judge",
            judge=True,
        )
    except Exception as e:  # noqa: BLE001 — Judge 실패를 '통과'로 보지 않는다
        verdict = EvalVerdict(passed=False, judge_used=False, fix_target="writer",
                              feedback=f"- LLM Judge 호출 실패: {e}", **rules)
        log_decision(trace_id, "evaluator", "FAIL", f"Judge 오류: {e}")
        return {"eval_result": verdict, "node_status": {"evaluator": "error"}, "last_error": f"judge: {e}"}

    criteria = {"groundedness": judge.groundedness, "neutrality": judge.neutrality,
                "bias_control": judge.bias_control, "coverage": judge.coverage}
    passed = all(c.passed for c in criteria.values())
    fix_target, rework = None, []
    if not passed:
        rework = [k for k in judge.evidence_gaps if k in config.PERSPECTIVES]
        if rework:
            # 근거 자체가 모자라면 그 근거를 모으는 담당에게 되돌린다
            needed = config.PERSPECTIVES[rework[0]]["required"]
            fix_target = COLLECTOR_BY_TYPE.get(needed[0], config.PERSPECTIVES[rework[0]]["owner"])
        else:
            fix_target = "writer"
    verdict = EvalVerdict(passed=passed, judge_used=True, fix_target=fix_target, rework_perspectives=rework,
                          feedback=judge.feedback or _feedback(criteria), **criteria)
    log_decision(trace_id, "evaluator", "PASS" if passed else "FAIL",
                 "규칙 검사 통과 + LLM Judge " + ("통과" if passed else f"미달 → {fix_target}"),
                 failed=[k for k, c in criteria.items() if not c.passed], rework_perspectives=rework)
    return {"eval_result": verdict, "node_status": {"evaluator": "ok"}}
