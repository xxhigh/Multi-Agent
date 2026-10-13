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
    "get_company_overview": "회사명: 주식회사 트위니 (Twinny)\n상장 구분: 기타 (비상장)\n대표자: 천영석, 천홍석\n설립일: 2015-09-01",
    "get_recent_disclosures": "[트위니 최근 1095일 DART 공시]\n2026-03-31 | 감사보고서 (2025.12) | 트위니 | "
                              "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260331001204",
    "search_company_docs": "[문서 풀 검색: 기업 자료 · 트위니 / '제품']\n나르고 오더피킹 최고 속도 1.5 m/s, 적재 100 kg. "
                           "3D LiDAR와 2D LiDAR 2개로 360° 장애물 감지. 2026-09 프리IPO 144억원 유치.\n[출처]\n"
                           "- 트위니 공식 자료 · 트위니 로봇 제품 소개서 — https://twinny.ai/brochure.pdf",
    "search_industry_docs": "[문서 풀 검색: 산업 자료 / '물류 로봇 시장']\n2024년 운송·물류 서비스 로봇 판매 102,900대(+14%). "
                            "글로벌 물류 로봇 2025년 177억 달러 → 2035년 870억 달러.\n[출처]\n"
                            "- IFR · World Robotics 2025 – Service Robots — https://ifr.org/service",
    "get_audit_financials": "[트위니 연결감사보고서 (2025.12)] (원본 표: 숫자·단위를 수정하지 말고 그대로 인용)\n"
                            "원문: https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260331001209\n\n"
                            "| 계정 | 2025 | 2024 |\n|---|---:|---:|\n| 매출액 | 50억 | 12억 |\n| 자본총계 | -546억 | -419억 |\n\n"
                            "■ 투자 판단 지표 (Python 계산)\n  매출 성장률: 316.1%\n  자본잠식: 완전자본잠식 (자본총계 < 0)",
    "search_audit_report": "[트위니 연결감사보고서 (2025.12) 검색: '상환전환우선주'] 원문: https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260331001209\n"
                           "당사가 발행한 복합금융상품은 보유자의 선택에 의해 보통주로 전환 또는 상환될 수 있는 상환전환우선주입니다.",
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
    monkeypatch.setattr(config, "MAX_PDF_PAGES", 50)  # 가짜 보고서는 쪽수 검사 대상이 아님
    script = {"calls": {}, "counts": defaultdict(int), "structured": {}, "text": [], "log": []}

    from finance_agent import llm
    from finance_agent.agents import _react, workers

    def fake_build(prompt, tools, max_tool_rounds=8):
        if "시장 리서처" in prompt:
            name = "market_researcher"
        elif "심사역" in prompt:
            name = "investment_analyst"
        else:
            name = "researcher"
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
