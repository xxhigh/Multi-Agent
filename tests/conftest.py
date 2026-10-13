"""API 키 없이 그래프 전체 흐름을 검증하기 위한 가짜 LLM·가짜 도구 실행기.

실제 모델 품질이 아니라 '라우팅·재작업·평가 루프·종료 보장'이 코드대로 동작하는지를 본다.
"""

import os
from collections import defaultdict

import pytest
from langchain_core.messages import AIMessage, ToolMessage

os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-used")

from finance_agent import config  # noqa: E402

TOOL_OUTPUTS = {
    "search_business_report": "[삼성전자 2025 사업보고서 검색: '사업 개요'] 원문: https://dart.fss.or.kr/dsaf001/main.do?rcpNo=1\n"
                              "DX·DS 부문으로 구성. DS 부문 매출 비중 40%. 연구개발비 30조.",
    "get_price_summary": "[삼성전자 주가 요약, 기간 1y]\n최근 종가: 80,000원\n기간 수익률: 12.00%",
    "get_company_overview": "회사명: 삼성전자 (SAMSUNG ELECTRONICS)\n종목코드: 005930, 시장: 코스피\n대표자: 홍길동",
    "get_recent_disclosures": "[삼성전자 최근 90일 공시]\n2026-09-01 | 분기보고서 | 삼성전자 | https://dart.fss.or.kr/dsaf001/main.do?rcpNo=2",
    "get_financial_statements": "[삼성전자 연결재무제표, annual] (원본 표: 숫자·단위를 수정하지 말고 그대로 인용)\n\n"
                                "| 계정 | 2025 | 2024 |\n|---|---:|---:|\n| 매출액 | 300.00조 | 280.00조 |",
    "calculate_financial_ratios": "[삼성전자 재무 비율, 2025 사업보고서 연결 기준]\n■ 성장성\n  매출액: 7.14%\n■ 수익성\n  영업이익률: 10.00%",
}


class FakeAgent:
    """ReAct 서브그래프 대역: 시나리오가 정한 도구 결과를 ToolMessage로 돌려준다."""

    def __init__(self, name: str, script: dict):
        self.name = name
        self.script = script

    def invoke(self, inputs, config=None):
        calls = self.script["calls"][self.name]
        idx = self.script["counts"][self.name]
        self.script["counts"][self.name] += 1
        tools = calls[min(idx, len(calls) - 1)]
        msgs = list(inputs["messages"])
        for i, tool in enumerate(tools):
            content = TOOL_OUTPUTS.get(tool, "") if not tool.startswith("!") else "오류: DART 응답 없음"
            name = tool.lstrip("!")
            msgs.append(AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": f"c{idx}{i}"}]))
            msgs.append(ToolMessage(content=content, name=name, tool_call_id=f"c{idx}{i}"))
        msgs.append(AIMessage(content=f"{self.name} 보고 {idx + 1}"))
        return {"messages": msgs}


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """script를 채워 넣으면 그에 따라 움직이는 가짜 LLM 환경."""
    monkeypatch.setattr(config, "RUN_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "REPORT_DIR", tmp_path / "reports")
    script = {"calls": {}, "counts": defaultdict(int), "structured": {}, "text": [], "log": []}

    from finance_agent import llm
    from finance_agent.agents import _react, workers

    def fake_build(prompt, tools, max_tool_rounds=8):
        name = "researcher" if "리서처" in prompt else "analyst"
        return FakeAgent(name, script)

    monkeypatch.setattr(_react, "build_react_agent", fake_build)
    monkeypatch.setattr(workers._react, "build_react_agent", fake_build)

    def fake_structured(schema, messages, *, run_name, judge=False):
        script["log"].append(run_name)
        handler = script["structured"][run_name]
        return handler(schema, messages)

    def fake_text(messages, *, run_name):
        script["log"].append(run_name)
        return script["text"].pop(0)

    monkeypatch.setattr(llm, "structured", fake_structured)
    monkeypatch.setattr(llm, "text", fake_text)
    return script
