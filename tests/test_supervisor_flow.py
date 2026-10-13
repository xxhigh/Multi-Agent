"""Supervisor 동적 라우팅·재작업·품질 평가 루프·종료 보장 시나리오 테스트 (가짜 LLM)."""

import re

from finance_agent import build_graph, config
from finance_agent.agents.evaluator import JudgeResult, rule_check
from finance_agent.agents.scorecard import ScoreDraft, ScoreDraftItem, compute
from finance_agent.agents.workers import PerspectiveFinding, WorkerFindings
from finance_agent.graph import route_after_supervisor
from finance_agent.observability import read_decisions
from finance_agent.runner import new_run, save_outputs
from finance_agent.state import CriterionResult
from finance_agent.supervisor import Assessment, IntakeResult, RoutingDecision

PAD = " 수치와 출처를 함께 서술한다." * 6
KEYS = r"(company_team|technology|market|investment)\("


def report_body(extra: str = "") -> str:
    return f"""# 트위니 투자 가능성 평가 보고서
## SUMMARY
트위니는 2015년 설립된 자율주행 물류로봇 기업이다 [E1]. 오더피킹 로봇은 1.5 m/s로 주행한다 [E3]. 물류 로봇 판매는 14% 늘었다 [E4]. 2025년 매출은 50억이다 [E5].{PAD}
## COMPANY & TEAM
대표자는 천영석·천홍석이다 [E1]. 2026-03-31 감사보고서를 공시했다 [E2]. 프리IPO 144억원을 유치했다 [E3].{PAD}{extra}
## TECHNOLOGY
3D LiDAR와 2D LiDAR로 장애물을 감지한다 [E3]. 감사보고서 공시 이력이 있다 [E2].{PAD}
## MARKET
2024년 운송·물류 서비스 로봇 판매는 102,900대다 [E4]. 회사는 물류센터 오더피킹 시장을 겨냥한다 [E3].{PAD}
## INVESTMENT ASSESSMENT & RISKS
### 핵심 리스크
자본총계는 -546억이다 [E5]. 상환전환우선주가 부채로 분류된다 [E6].{PAD}
"""


def structuring_handler(schema, messages):
    system = messages[0].content
    targets = re.search(r"담당 관점: (.+)", system).group(1)
    keys = re.findall(KEYS, targets)
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
        keys = re.findall(r"company_team|technology|market|investment", judged)
        pref = prefs[min(calls["n"], len(prefs) - 1)]
        calls["n"] += 1
        return RoutingDecision(
            assessments=[Assessment(perspective=k, sufficient=True, reason="충분") for k in keys],
            next=pref if pref in allowed else allowed[0],
            instruction=f"{pref} 작업 지시", reason=f"테스트 선택 {pref}")
    return handler


def score_handler(schema, messages):
    ids = re.findall(r"- (E\d+) \[", messages[0].content)
    return ScoreDraft(items=[ScoreDraftItem(key="market", score=4, rationale="물류 로봇 판매 14% 증가", evidence_ids=ids[:1]),
                             ScoreDraftItem(key="traction", score=2, rationale="자본잠식", evidence_ids=ids[-1:]),
                             ScoreDraftItem(key="founder", score=None, rationale="근거 부족")])


def judge_pass(schema, messages):
    ok = CriterionResult(passed=True)
    return JudgeResult(groundedness=ok, neutrality=ok, bias_control=ok, coverage=ok)


def base_structured(prefs, judge=None):
    return {
        "supervisor_intake": lambda s, m: IntakeResult(in_scope=True, company="트위니", reason="로보틱스 스타트업"),
        "supervisor_routing": routing_handler(prefs),
        "researcher_structuring": structuring_handler,
        "market_researcher_structuring": structuring_handler,
        "investment_analyst_structuring": structuring_handler,
        "scorecard": score_handler,
        "quality_judge": judge or judge_pass,
    }


ALL_OK = {
    "researcher": [["get_company_overview", "get_recent_disclosures", "search_company_docs"]],
    "market_researcher": [["search_industry_docs"]],
    "investment_analyst": [["get_audit_financials", "search_audit_report"]],
}


def run(question="트위니 투자 가능성 평가해줘"):
    graph = build_graph()
    trace_id, state, cfg = new_run(question)
    final = graph.invoke(state, cfg)
    final["_paths"] = save_outputs(trace_id, final)
    return trace_id, final


def test_dynamic_rework_and_revision_loop(harness):
    """researcher→market(산업자료 실패)→investment→market 재작업→writer→중립성 미달→수정→통과."""
    harness["calls"] = {**ALL_OK, "market_researcher": [["!search_industry_docs"], ["search_industry_docs"]]}
    harness["structured"] = base_structured(["researcher", "market_researcher", "investment_analyst", "market_researcher"])
    harness["text"] = [report_body(" 향후 성장이 유망하다 [E1]."), report_body()]

    trace_id, final = run()

    decisions = read_decisions(trace_id)
    routes = [d["decision"] for d in decisions if d["node"] == "supervisor" and d["decision"] != "intake"]
    assert routes == ["researcher", "market_researcher", "investment_analyst", "market_researcher",
                      "writer", "evaluator", "writer", "evaluator", "FINISH"]
    # 산업 자료(industry_doc) 부족으로 market 관점 재작업이 실제로 일어났다
    rework = [d for d in decisions if d["node"] == "supervisor" and d.get("is_rework")]
    assert any("market" in (d.get("targets") or []) for d in rework)
    assert final["attempts"]["market"] == 2
    # 1차 초안은 금지 표현으로 규칙 검사 실패 → Judge 생략, 2차 초안은 통과
    evals = [d for d in decisions if d["node"] == "evaluator"]
    assert [d["decision"] for d in evals] == ["FAIL", "PASS"]
    assert harness["log"].count("quality_judge") == 1
    assert final["status"] == "completed"
    assert final["revision_count"] == 1
    assert all(p.sufficient for p in final["perspectives"].values())
    # 스코어카드·재무표·REFERENCE는 코드가 삽입
    report = final["report"]
    assert "### 투자 평가 스코어카드" in report and "결측" in report
    assert "### 재무 요약 (DART 감사보고서 원본 표)" in report and "| 자본총계 | -546억 | -419억 |" in report
    assert "## REFERENCE" in report and "[E6]" in report
    assert final["scorecard"].covered_weight == 40  # 시장성 30 + 실적 10 (창업자는 결측)
    # 원문은 파일, State에는 참조만
    assert all(e.path.endswith(".md") for e in final["evidence"])
    # 제출용 PDF도 함께 생성되고 한글 본문이 들어 있다
    import pymupdf
    pdf = final["_paths"]["pdf"]
    assert pdf.suffix == ".pdf" and pdf.exists()
    with pymupdf.open(str(pdf)) as doc:
        text = "".join(page.get_text() for page in doc)
    assert "COMPANY & TEAM" in text and "REFERENCE" in text and "트위니" in text


def test_termination_when_evidence_never_sufficient(harness):
    """도구가 계속 실패해도 관점별 재작업 상한 → writer(한계 명시) → 수정 상한 → 경고 종료."""
    harness["calls"] = {"researcher": [["!search_company_docs"]], "market_researcher": [["!search_industry_docs"]],
                        "investment_analyst": [["!get_audit_financials"]]}
    harness["structured"] = base_structured(["researcher", "market_researcher", "investment_analyst"])
    harness["text"] = ["# 짧은 보고서\n## SUMMARY\n자료 부족"] * 5

    trace_id, final = run()

    assert final["status"] == "completed_with_warning"
    assert final["step_count"] <= config.MAX_STEPS + 1
    assert all(v <= config.MAX_ATTEMPTS for v in final["attempts"].values())
    assert final["revision_count"] == config.MAX_REVISIONS
    assert all(p.limited for p in final["perspectives"].values())
    assert final["report"].startswith("> ⚠️ 품질 경고")


def test_judge_evidence_gap_sends_back_to_collector(harness):
    """LLM Judge가 '근거 자체 부족'으로 판정하면 writer가 아니라 그 근거의 수집 담당에게 되돌린다."""
    harness["calls"] = ALL_OK
    judge_calls = {"n": 0}

    def judge_gap_then_pass(schema, messages):
        judge_calls["n"] += 1
        if judge_calls["n"] == 1:
            bad = CriterionResult(passed=False, issues=["시장 근거가 1개뿐"])
            ok = CriterionResult(passed=True)
            return JudgeResult(groundedness=ok, neutrality=ok, bias_control=bad, coverage=ok,
                               evidence_gaps=["market"], feedback="- 시장 근거 보강")
        return judge_pass(schema, messages)

    harness["structured"] = base_structured(["researcher", "market_researcher", "investment_analyst"],
                                            judge_gap_then_pass)
    harness["text"] = [report_body(), report_body()]

    trace_id, final = run()

    routes = [d["decision"] for d in read_decisions(trace_id) if d["node"] == "supervisor"]
    assert routes[1:] == ["researcher", "market_researcher", "investment_analyst", "writer", "evaluator",
                          "market_researcher", "writer", "evaluator", "FINISH"]
    gap_route = [d for d in read_decisions(trace_id) if d["node"] == "supervisor" and "재조사" in d["reason"]]
    assert gap_route and gap_route[0]["decision"] == "market_researcher"
    assert final["status"] == "completed"


def test_out_of_scope(harness):
    harness["structured"] = {
        "supervisor_intake": lambda s, m: IntakeResult(in_scope=False, company="", reason="로보틱스 무관 기업"),
    }
    trace_id, final = run("삼성생명 투자 평가해줘")
    assert final["status"] == "out_of_scope"
    assert final["next"] == "FINISH"


def test_route_function_is_pure_gate():
    assert route_after_supervisor({"next": "writer", "step_count": 3, "max_steps": 16}) == "writer"
    assert route_after_supervisor({"next": "FINISH_WARNING"}) == "end"
    assert route_after_supervisor({"next": "researcher", "step_count": 99, "max_steps": 16}) == "end"
    assert route_after_supervisor({"next": "unknown"}) == "end"


def test_rule_check_detects_neutrality_and_missing_sections():
    results = rule_check("# 제목\n## SUMMARY\n투자 추천합니다 [E1]", [], {})
    assert not results["neutrality"].passed
    assert not results["coverage"].passed
    assert not results["groundedness"].passed


def test_cli_and_backend_stream_with_fake_llm(harness, capsys):
    """CLI 출력·SSE 이벤트 변환이 새 State 구조에서 깨지지 않는지 확인."""
    import importlib
    import json as _json

    harness["calls"] = ALL_OK
    harness["structured"] = base_structured(["researcher", "market_researcher", "investment_analyst"])
    harness["text"] = [report_body()]
    backend = importlib.import_module("backend.app")
    backend = importlib.reload(backend)
    events = [e for e in backend._events("트위니 투자 가능성 평가해줘", "t-1")]
    kinds = [e.split("\n")[0].removeprefix("event: ") for e in events]
    assert kinds[0] == "start" and kinds[-1] == "done"
    assert "report" in kinds and "error" not in kinds
    routes = [_json.loads(e.split("data: ", 1)[1]) for e in events if e.startswith("event: route")]
    assert [r["next"] for r in routes][-1] == "FINISH"

    import main
    harness["text"] = [report_body()]
    harness["counts"].clear()
    main.run(build_graph(), "트위니 투자 가능성 평가해줘")
    out = capsys.readouterr().out
    assert "supervisor → researcher" in out and "evaluator: PASS" in out and "상태: completed" in out


def test_scorecard_math_and_missing_items():
    """가중합은 코드가 계산하고, 근거 없는 점수는 결측으로 분모에서 뺀다."""
    draft = ScoreDraft(items=[
        ScoreDraftItem(key="market", score=5, rationale="a", evidence_ids=["E1"]),
        ScoreDraftItem(key="product_tech", score=3, rationale="b", evidence_ids=["E2"]),
        ScoreDraftItem(key="traction", score=4, rationale="근거 ID 없음", evidence_ids=[]),
        ScoreDraftItem(key="deal_terms", score=9, rationale="범위 밖", evidence_ids=["E1"]),
    ])
    sc = compute(draft, {"E1", "E2"})
    assert sc.covered_weight == 55
    assert sc.total == round((30 * 5 + 25 * 3) / (5 * 55) * 100, 1)
    assert [i.score for i in sc.items if i.key in ("traction", "deal_terms", "founder")] == [None, None, None]


def test_audit_parser_reads_statement_rows():
    """감사보고서 표 행에서 (당기, 전기) 값을 원 단위로 읽고 '손실' 계정의 부호를 맞춘다."""
    from finance_agent.tools import audit
    text = ("(단위 : 원)\n과 목 | 주석 | 제11(당)기 | 제10(전)기\n"
            "Ⅰ. 매출액 | 25 | 5,035,485,094 | 1,210,037,418\n"
            "Ⅲ. 영업손실 | | (6,606,080,044) | (11,418,006,377)\n"
            "자본총계 | | (54,638,570,954) | (41,917,894,831)\n")
    fin, unit = audit.extract_financials(text)
    assert unit == 1
    assert fin["매출액"] == (5035485094, 1210037418)
    assert fin["영업이익"] == (-6606080044, -11418006377)
    assert fin["자본총계"][0] == -54638570954
