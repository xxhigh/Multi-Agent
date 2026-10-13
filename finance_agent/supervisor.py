"""Supervisor 노드: 현재 State를 보고 다음 담당자를 정한다.

판단(확률)과 분기(결정론)를 분리한다.
- 판단: LLM이 관점별 근거 충분성을 평가하고, '허용된 후보' 중에서 다음 담당자와 지시를 고른다.
- 게이트: 허용 후보 계산·상한 검사·재작업 횟수는 코드가 정한다. LLM이 후보 밖을 고르면 코드가 덮어쓰고 로그를 남긴다.
- 분기: graph.py의 route_after_supervisor()가 State의 next만 읽어 add_conditional_edges로 이동한다.
"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from . import config, llm
from .observability import log_decision
from .prompts import INTAKE_PROMPT, ROUTING_PROMPT
from .state import FinanceState, PerspectiveKey, PerspectiveResult
from .tools import COLLECTOR_BY_TYPE

_WORKER_ORDER = ("researcher", "analyst", "writer")


class IntakeResult(BaseModel):
    in_scope: bool
    company: str = Field(description="분석 대상 기업명 (범위 밖이면 빈 문자열)")
    reason: str


class Assessment(BaseModel):
    perspective: PerspectiveKey
    sufficient: bool
    reason: str


class RoutingDecision(BaseModel):
    assessments: list[Assessment]
    next: str = Field(description="허용된 후보 중 하나")
    instruction: str
    reason: str


# ── 규칙 기반 충분성 (결정론) ──────────────────────────────────


def rule_check(key: str, p: PerspectiveResult | None, types_present: set[str]) -> tuple[bool, list[str], str]:
    """(통과 여부, 없는 근거 종류, 사유)."""
    required = config.PERSPECTIVES[key]["required"]
    missing = [t for t in required if t not in types_present]
    if missing:
        return False, missing, f"필수 근거 없음: {', '.join(missing)}"
    if p is None or not p.findings.strip():
        return False, [], "정리된 분석 내용 없음"
    if not p.evidence_ids:
        return False, [], "분석 내용에 근거 ID가 연결되지 않음"
    return True, [], "필수 근거·내용·인용 확인"


def responsible_workers(key: str, missing: list[str]) -> set[str]:
    """이 관점을 보강할 수 있는 담당자: 없는 근거의 수집 담당 + (근거가 있으면) 작성 담당."""
    if missing:
        return {COLLECTOR_BY_TYPE[t] for t in missing}
    return {config.PERSPECTIVES[key]["owner"]}


def _summary_for_llm(state: FinanceState, rule_results: dict, attempts: dict) -> str:
    persp = state.get("perspectives") or {}
    lines = [f"분석 대상: {state['company']}", f"사용자 요청: {state['question']}", ""]
    for key, spec in config.PERSPECTIVES.items():
        ok, _, reason = rule_results[key]
        p = persp.get(key)
        lines.append(f"[{key}] {spec['title']} — 규칙 검사: {'통과' if ok else '부족'} ({reason}), "
                     f"시도 {attempts.get(key, 0)}/{config.MAX_ATTEMPTS}회")
        if p and p.findings:
            lines.append(f"  내용: {p.findings[:400]}")
            lines.append(f"  근거 ID: {', '.join(p.evidence_ids) or '-'}")
        if p and p.gaps:
            lines.append(f"  부족한 점: {'; '.join(p.gaps[-3:])}")
    return "\n".join(lines)


def _perspective_guide() -> str:
    return "\n".join(f"- {k}: {v['title']} (노션 관점 '{v['notion_view']}') — {v['guide']}"
                     for k, v in config.PERSPECTIVES.items())


def _finish(state: FinanceState, step: int, warning: str | None) -> dict:
    report = state.get("report") or ""
    status = "completed"
    if warning:
        status = "completed_with_warning"
        report = f"> ⚠️ 품질 경고: {warning}\n\n{report}"
    decision = "FINISH_WARNING" if warning else "FINISH"
    log_decision(state["trace_id"], "supervisor", decision, warning or "품질 평가 통과", step=step)
    return {"step_count": step, "next": decision, "report": report, "status": status,
            "messages": [AIMessage(content=report, name="writer")]}


def supervisor_node(state: FinanceState) -> dict:
    trace_id = state["trace_id"]
    step = (state.get("step_count") or 0) + 1
    max_steps = state.get("max_steps") or config.MAX_STEPS
    update: dict = {"step_count": step}

    # ── 0. 접수: 대상 기업·지원 범위 확인 ────────────────────────
    company = state.get("company")
    if not company:
        intake: IntakeResult = llm.structured(
            IntakeResult, [SystemMessage(INTAKE_PROMPT), HumanMessage(state["question"])],
            run_name="supervisor_intake")
        if not intake.in_scope or not intake.company.strip():
            notice = (f"요청하신 내용은 지원 범위가 아닙니다. ({intake.reason})\n"
                      "이 에이전트는 국내 상장사 1곳의 재무·사업 분석 보고서를 작성합니다.")
            log_decision(trace_id, "supervisor", "FINISH", f"범위 밖 요청: {intake.reason}", step=step)
            return {**update, "next": "FINISH", "status": "out_of_scope", "report": notice,
                    "messages": [AIMessage(content=notice, name="supervisor")]}
        company = intake.company.strip()
        update["company"] = company
        state = {**state, "company": company}
        log_decision(trace_id, "supervisor", "intake", f"분석 대상: {company}", step=step)

    report = state.get("report")
    verdict = state.get("eval_result")
    revisions = state.get("revision_count") or 0

    # ── 1. 종료 가드 (코드 강제) ────────────────────────────────
    if step > max_steps:
        if report:
            return {**update, **_finish(state, step, f"단계 상한({max_steps}) 도달로 품질 평가를 마치지 못하고 종료")}
        persp = state.get("perspectives") or {}
        update["perspectives"] = {k: (persp.get(k) or PerspectiveResult(view=k)).model_copy(
            update={"limited": True}) for k in config.PERSPECTIVE_KEYS
            if not (persp.get(k) and persp[k].sufficient)}
        log_decision(trace_id, "supervisor", "writer", f"단계 상한({max_steps}) 도달 — 확보된 근거로 작성",
                     step=step, forced=True)
        return {**update, "next": "writer", "instruction": "확보된 근거만으로 작성하고 부족한 관점은 한계로 명시",
                "target_perspectives": []}

    # ── 2. 보고서 평가 결과가 있으면: 통과/수정/재조사/경고 종료 ────
    if report and verdict is not None:
        if verdict.passed:
            return {**update, **_finish(state, step, None)}
        if revisions >= config.MAX_REVISIONS:
            return {**update, **_finish(state, step, f"수정 {revisions}회 후에도 품질 기준 미달: "
                                                      f"{verdict.feedback[:200]}")}
        attempts = state.get("attempts") or {}
        rework = [k for k in verdict.rework_perspectives if attempts.get(k, 0) < config.MAX_ATTEMPTS]
        if verdict.fix_target in ("researcher", "analyst") and rework:
            persp = state.get("perspectives") or {}
            marked = {k: (persp.get(k) or PerspectiveResult(view=k)).model_copy(update={
                "sufficient": False, "gaps": [f"품질 평가: {verdict.feedback[:200]}"]}) for k in rework}
            targets = rework
            log_decision(trace_id, "supervisor", verdict.fix_target,
                         f"품질 평가 근거 부족 → 재조사 {targets}", step=step, is_rework=True,
                         targets=targets, revision=revisions + 1)
            return {**update, "next": verdict.fix_target, "perspectives": marked, "report": None,
                    "eval_result": None, "revision_count": revisions + 1, "target_perspectives": targets,
                    "attempts": {k: attempts.get(k, 0) + 1 for k in targets},
                    "instruction": f"품질 평가에서 근거 부족 지적: {verdict.feedback[:300]}"}
        log_decision(trace_id, "supervisor", "writer", "품질 평가 미달 → 보고서 수정 요청",
                     step=step, is_rework=True, revision=revisions + 1)
        return {**update, "next": "writer", "revision_count": revisions + 1,
                "instruction": verdict.feedback, "target_perspectives": []}

    # ── 3. 초안이 있고 아직 평가 전이면: 품질 평가 노드로 ───────────
    if report:
        log_decision(trace_id, "supervisor", "evaluator", "보고서 초안 작성 완료 → 품질 평가", step=step)
        return {**update, "next": "evaluator", "instruction": "", "target_perspectives": []}

    # ── 4. 근거 수집 단계: 관점별 충분성 평가 → 재작업 or 작성 ────────
    persp = dict(state.get("perspectives") or {})
    attempts = dict(state.get("attempts") or {})
    types_present = {e.type for e in (state.get("evidence") or [])}
    rule_results = {k: rule_check(k, persp.get(k), types_present) for k in config.PERSPECTIVE_KEYS}

    # LLM 판정이 필요한 관점: 규칙은 통과했지만 아직 '충분' 판정을 받지 않은 것
    to_judge = [k for k, (ok, _, _) in rule_results.items() if ok and not (persp.get(k) and persp[k].sufficient)]
    pre_insufficient = [k for k, (ok, _, _) in rule_results.items() if not ok]
    pre_allowed = set()
    for k in pre_insufficient + to_judge:
        if attempts.get(k, 0) < config.MAX_ATTEMPTS:
            pre_allowed |= responsible_workers(k, rule_results[k][1])
    if not pre_insufficient:
        pre_allowed.add("writer")

    decision: RoutingDecision | None = None
    if to_judge or len(pre_allowed) > 1:
        allowed_text = ", ".join(w for w in _WORKER_ORDER if w in pre_allowed) or "writer"
        try:
            decision = llm.structured(
                RoutingDecision,
                [SystemMessage(ROUTING_PROMPT.format(perspective_guide=_perspective_guide())),
                 HumanMessage(f"## 현재 상태\n{_summary_for_llm(state, rule_results, attempts)}\n\n"
                              f"## 허용된 다음 담당자\n{allowed_text}\n(판정 대상 관점: {', '.join(to_judge) or '없음'})")],
                run_name="supervisor_routing")
        except Exception as e:  # noqa: BLE001 — 판단 실패 시 규칙만으로 진행
            log_decision(trace_id, "supervisor", "llm_error", f"라우팅 판단 실패, 규칙으로 진행: {e}", step=step)

    llm_view = {a.perspective: a for a in (decision.assessments if decision else [])}
    insufficient, persp_updates = [], {}
    for key in config.PERSPECTIVE_KEYS:
        ok, missing, reason = rule_results[key]
        p = persp.get(key) or PerspectiveResult(view=key)
        sufficient = ok and (p.sufficient or (llm_view[key].sufficient if key in llm_view else True))
        if ok and key in llm_view and not llm_view[key].sufficient:
            reason = f"LLM 판정 부족: {llm_view[key].reason}"
        elif ok and key in llm_view:
            reason = llm_view[key].reason
        exhausted = attempts.get(key, 0) >= config.MAX_ATTEMPTS
        gaps = p.gaps if sufficient else (p.gaps + [reason])[-4:]
        persp_updates[key] = p.model_copy(update={
            "sufficient": sufficient, "sufficiency_reason": reason,
            "limited": (not sufficient) and exhausted, "gaps": gaps})
        if not sufficient and not exhausted:
            insufficient.append((key, missing))

    allowed = set()
    for key, missing in insufficient:
        allowed |= responsible_workers(key, missing)
    if not allowed:
        allowed = {"writer"}

    chosen = decision.next if decision else None
    override = None
    if chosen not in allowed:
        override = chosen
        chosen = next(w for w in _WORKER_ORDER if w in allowed)

    sufficiency_log = {k: persp_updates[k].sufficient for k in config.PERSPECTIVE_KEYS}
    update["perspectives"] = persp_updates

    if chosen == "writer":
        limited = [k for k, p in persp_updates.items() if p.limited]
        reason = "4개 관점 근거 충분" if not limited else f"재작업 한도 소진 관점 {limited}은 한계로 명시하고 작성"
        log_decision(trace_id, "supervisor", "writer", reason, step=step, sufficiency=sufficiency_log,
                     llm_override=override)
        return {**update, "next": "writer", "target_perspectives": [],
                "instruction": decision.instruction if decision and not override else reason}

    targets = [k for k, missing in insufficient if chosen in responsible_workers(k, missing)]
    is_rework = any(attempts.get(k, 0) >= 1 for k in targets)
    instruction = (decision.instruction if decision and not override else
                   "다음 관점의 부족한 근거를 보강하세요: " +
                   "; ".join(f"{config.PERSPECTIVES[k]['title']}({persp_updates[k].sufficiency_reason})"
                             for k in targets))
    log_decision(trace_id, "supervisor", chosen,
                 (decision.reason if decision and not override else "규칙 기반 기본 경로")
                 + (f" [LLM 선택 '{override}'는 허용 후보 밖이라 교체]" if override else ""),
                 step=step, targets=targets, is_rework=is_rework, sufficiency=sufficiency_log)
    return {**update, "next": chosen, "target_perspectives": targets, "instruction": instruction,
            "attempts": {k: attempts.get(k, 0) + 1 for k in targets}}
