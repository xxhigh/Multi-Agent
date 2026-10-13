"""Supervisor 동적 라우팅·재작업·품질 평가 루프·종료 보장 시나리오 테스트 (가짜 LLM)."""

import re

from finance_agent import build_graph, config
from finance_agent.agents.evaluator import JudgeResult, rule_check
from finance_agent.agents.workers import PerspectiveFinding, WorkerFindings
from finance_agent.graph import route_after_supervisor
from finance_agent.observability import read_decisions
from finance_agent.runner import new_run, save_outputs
from finance_agent.state import CriterionResult
from finance_agent.supervisor import Assessment, IntakeResult, RoutingDecision

PAD = " 수치와 출처를 함께 서술한다." * 6


def report_body(extra: str = "") -> str:
    return f"""# 삼성전자 재무 분석 보고서
## SUMMARY
사업 구조는 DX·DS 부문으로 나뉜다 [E1]. 매출은 7.14% 증가했다 [E5]. 최근 분기보고서가 공시됐다 [E6]. 영업이익률은 10%다 [E5].{PAD}
## 1. 사업·기술 경쟁력
DS 부문 매출 비중은 40%이고 연구개발비는 30조다 [E1]. 코스피 상장사다 [E3].{PAD}{extra}
## 2. 성장성·시장 평가
최근 1년 주가 수익률은 12%다 [E2]. 매출액 성장률은 7.14%다 [E5].{PAD}
## 3. 주주·지배구조·공시
대표자는 홍길동이다 [E3]. 2026-09-01 분기보고서를 공시했다 [E6].{PAD}
## 4. 재무 건전성·산업 특성
매출액은 300조다 [E4]. 영업이익률은 10%다 [E5].{PAD}
## 재무제표 (원본 표)
### 연간 [E4]
| 계정 | 2025 | 2024 |
## 한계 및 확인 필요 사항
현금흐름 세부 항목은 추가 확인이 필요하다.{PAD}
"""


def structuring_handler(schema, messages):
    system = messages[0].content
    targets = re.search(r"담당 관점: (.+)", system).group(1)
    keys = re.findall(r"(business_tech|market|stakeholder|financial_health)\(", targets)
    ids = re.findall(r"- (E\d+) \[", system)
    return WorkerFindings(items=[PerspectiveFinding(perspective=k, findings=f"{k} 관련 사실 정리", evidence_ids=ids)
                                 for k in keys])


def routing_handler(prefs):
    """prefs: 호출마다 선호하는 next. 허용 후보에 없으면 supervisor가 덮어쓴다."""
    calls = {"n": 0}

    def handler(schema, messages):
        human = messages[-1].content
        allowed = re.search(r"## 허용된 다음 담당자\n(.+)", human).group(1).split(", ")
        judged = re.search(r"판정 대상 관점: (.+)\)", human).group(1)
        keys = re.findall(r"business_tech|market|stakeholder|financial_health", judged)
        pref = prefs[min(calls["n"], len(prefs) - 1)]
        calls["n"] += 1
        return RoutingDecision(
            assessments=[Assessment(perspective=k, sufficient=True, reason="충분") for k in keys],
            next=pref if pref in allowed else allowed[0],
            instruction=f"{pref} 작업 지시", reason=f"테스트 선택 {pref}")
    return handler


def judge_pass(schema, messages):
    ok = CriterionResult(passed=True)
    return JudgeResult(groundedness=ok, neutrality=ok, bias_control=ok, coverage=ok)


def run(question="삼성전자 재무 분석해줘"):
    graph = build_graph()
    trace_id, state, cfg = new_run(question)
    final = graph.invoke(state, cfg)
    final["_paths"] = save_outputs(trace_id, final)
    return trace_id, final


def test_dynamic_rework_and_revision_loop(harness):
    """researcher→analyst→(공시 부족) researcher 재작업→writer→중립성 미달→수정→통과."""
    harness["calls"] = {
        "researcher": [
            ["search_business_report", "get_price_summary", "get_company_overview", "get_financial_statements"],
            ["get_recent_disclosures"],
        ],
        "analyst": [["calculate_financial_ratios"]],
    }
    harness["structured"] = {
        "supervisor_intake": lambda s, m: IntakeResult(in_scope=True, company="삼성전자", reason="국내 상장사"),
        "supervisor_routing": routing_handler(["researcher", "analyst", "researcher"]),
        "researcher_structuring": structuring_handler,
        "analyst_structuring": structuring_handler,
        "quality_judge": judge_pass,
    }
    harness["text"] = [report_body(" 향후 성장이 유망하다 [E1]."), report_body()]

    trace_id, final = run()

    decisions = read_decisions(trace_id)
    routes = [d["decision"] for d in decisions if d["node"] == "supervisor" and d["decision"] != "intake"]
    assert routes == ["researcher", "analyst", "researcher", "writer", "evaluator", "writer", "evaluator", "FINISH"]
    # 공시(disclosure) 부족으로 stakeholder 재작업이 실제로 일어났다
    rework = [d for d in decisions if d["node"] == "supervisor" and d.get("is_rework")]
    assert any("stakeholder" in (d.get("targets") or []) for d in rework)
    assert final["attempts"]["stakeholder"] == 2
    # 1차 초안은 금지 표현으로 규칙 검사 실패 → Judge 생략, 2차 초안은 통과
    evals = [d for d in decisions if d["node"] == "evaluator"]
    assert [d["decision"] for d in evals] == ["FAIL", "PASS"]
    assert harness["log"].count("quality_judge") == 1
    assert final["status"] == "completed"
    assert final["revision_count"] == 1
    assert "## REFERENCE" in final["report"] and "[E6]" in final["report"]
    assert all(p.sufficient for p in final["perspectives"].values())
    # 원문은 파일, State에는 참조만
    assert all(e.path.endswith(".md") for e in final["evidence"])
    # 제출용 PDF도 함께 생성되고 한글 본문이 들어 있다
    import pymupdf
    pdf = final["_paths"]["pdf"]
    assert pdf.suffix == ".pdf" and pdf.exists()
    with pymupdf.open(str(pdf)) as doc:
        text = "".join(page.get_text() for page in doc)
    assert "사업·기술 경쟁력" in text and "REFERENCE" in text


def test_termination_when_evidence_never_sufficient(harness):
    """도구가 계속 실패해도 관점별 재작업 상한 → writer(한계 명시) → 수정 상한 → 경고 종료."""
    harness["calls"] = {"researcher": [["!search_business_report"]], "analyst": [["!calculate_financial_ratios"]]}
    harness["structured"] = {
        "supervisor_intake": lambda s, m: IntakeResult(in_scope=True, company="삼성전자", reason="ok"),
        "supervisor_routing": routing_handler(["researcher", "analyst"]),
        "researcher_structuring": structuring_handler,
        "analyst_structuring": structuring_handler,
        "quality_judge": judge_pass,
    }
    harness["text"] = ["# 짧은 보고서\n## SUMMARY\n자료 부족"] * 5

    trace_id, final = run()

    assert final["status"] == "completed_with_warning"
    assert final["step_count"] <= config.MAX_STEPS + 1
    assert all(v <= config.MAX_ATTEMPTS for v in final["attempts"].values())
    assert final["revision_count"] == config.MAX_REVISIONS
    assert all(p.limited for p in final["perspectives"].values())
    assert final["report"].startswith("> ⚠️ 품질 경고")


def test_judge_evidence_gap_sends_back_to_collector(harness):
    """LLM Judge가 '근거 자체 부족'으로 판정하면 writer가 아니라 수집 담당에게 되돌린다."""
    harness["calls"] = {
        "researcher": [["search_business_report", "get_price_summary", "get_company_overview",
                        "get_financial_statements", "get_recent_disclosures"]],
        "analyst": [["calculate_financial_ratios"]],
    }
    judge_calls = {"n": 0}

    def judge_gap_then_pass(schema, messages):
        judge_calls["n"] += 1
        if judge_calls["n"] == 1:
            bad = CriterionResult(passed=False, issues=["사업 구조 근거가 1개뿐"])
            ok = CriterionResult(passed=True)
            return JudgeResult(groundedness=ok, neutrality=ok, bias_control=bad, coverage=ok,
                               evidence_gaps=["business_tech"], feedback="- 사업 근거 보강")
        return judge_pass(schema, messages)

    harness["structured"] = {
        "supervisor_intake": lambda s, m: IntakeResult(in_scope=True, company="삼성전자", reason="ok"),
        "supervisor_routing": routing_handler(["researcher", "analyst"]),
        "researcher_structuring": structuring_handler,
        "analyst_structuring": structuring_handler,
        "quality_judge": judge_gap_then_pass,
    }
    body = report_body().replace("[E6]", "[E5]")
    harness["text"] = [body, body]

    trace_id, final = run()

    routes = [d["decision"] for d in read_decisions(trace_id) if d["node"] == "supervisor"]
    assert routes[1:] == ["researcher", "analyst", "writer", "evaluator",
                          "researcher", "writer", "evaluator", "FINISH"]
    gap_route = [d for d in read_decisions(trace_id) if d["node"] == "supervisor" and "재조사" in d["reason"]]
    assert gap_route and gap_route[0]["decision"] == "researcher"
    assert final["status"] == "completed"


def test_out_of_scope(harness):
    harness["structured"] = {
        "supervisor_intake": lambda s, m: IntakeResult(in_scope=False, company="", reason="해외 기업"),
    }
    trace_id, final = run("엔비디아 재무 분석해줘")
    assert final["status"] == "out_of_scope"
    assert final["next"] == "FINISH"


def test_route_function_is_pure_gate():
    assert route_after_supervisor({"next": "writer", "step_count": 3, "max_steps": 16}) == "writer"
    assert route_after_supervisor({"next": "FINISH_WARNING"}) == "end"
    assert route_after_supervisor({"next": "researcher", "step_count": 99, "max_steps": 16}) == "end"
    assert route_after_supervisor({"next": "unknown"}) == "end"


def test_rule_check_detects_neutrality_and_missing_sections():
    results = rule_check("# 제목\n## SUMMARY\n매수 추천합니다 [E1]", [], {})
    assert not results["neutrality"].passed
    assert not results["coverage"].passed
    assert not results["groundedness"].passed


def test_cli_and_backend_stream_with_fake_llm(harness, capsys):
    """CLI 출력·SSE 이벤트 변환이 새 State 구조에서 깨지지 않는지 확인."""
    import importlib
    import json as _json

    harness["calls"] = {
        "researcher": [["search_business_report", "get_price_summary", "get_company_overview",
                        "get_financial_statements", "get_recent_disclosures"]],
        "analyst": [["calculate_financial_ratios"]],
    }
    harness["structured"] = {
        "supervisor_intake": lambda s, m: IntakeResult(in_scope=True, company="삼성전자", reason="ok"),
        "supervisor_routing": routing_handler(["researcher", "analyst"]),
        "researcher_structuring": structuring_handler,
        "analyst_structuring": structuring_handler,
        "quality_judge": judge_pass,
    }
    harness["text"] = [report_body()]
    backend = importlib.import_module("backend.app")
    backend = importlib.reload(backend)
    events = [e for e in backend._events("삼성전자 재무 분석해줘", "t-1")]
    kinds = [e.split("\n")[0].removeprefix("event: ") for e in events]
    assert kinds[0] == "start" and kinds[-1] == "done"
    assert "report" in kinds and "error" not in kinds
    routes = [_json.loads(e.split("data: ", 1)[1]) for e in events if e.startswith("event: route")]
    assert [r["next"] for r in routes][-1] == "FINISH"

    import main
    harness["text"] = [report_body()]
    harness["counts"].clear()
    main.run(build_graph(), "삼성전자 재무 분석해줘")
    out = capsys.readouterr().out
    assert "supervisor → researcher" in out and "evaluator: PASS" in out and "상태: completed" in out
