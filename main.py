"""재무 분석 멀티 agent CLI.

사용법:
    python main.py                          # 대화형 모드
    python main.py "셀트리온 재무 분석해줘"    # 단일 질문
    python main.py --graph                  # 그래프 구조 이미지(docs/images/graph.png) 생성
"""

import sys
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from finance_agent import build_graph  # noqa: E402
from finance_agent.observability import OUTPUT_DIR, langsmith_enabled, log_decision, run_config  # noqa: E402

REPORT_DIR = OUTPUT_DIR / "reports"


def save_report(report: str, trace_id: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"report_{datetime.now():%Y%m%d_%H%M%S}.md"
    path.write_text(report, encoding="utf-8")
    log_decision(trace_id, "output", "saved", path=path.name)  # 리포트 파일 ↔ trace_id 연결
    return path


def run(graph, question: str, thread_id: str) -> None:
    trace_id = uuid.uuid4().hex
    config = run_config(thread_id, trace_id, question)
    print(f"  🔗 trace_id={trace_id}" + (" (LangSmith 기록 중)" if langsmith_enabled() else ""))
    stream = graph.stream({"messages": [("user", question)], "trace_id": trace_id}, config,
                          stream_mode="updates", subgraphs=True)
    for namespace, update in stream:
        for node, data in update.items():
            if not data:
                continue
            if namespace:  # researcher/analyst 내부의 도구 호출 표시
                if node == "agent" and data["messages"][-1].tool_calls:
                    for call in data["messages"][-1].tool_calls:
                        args = ", ".join(f"{k}={v}" for k, v in call["args"].items())
                        print(f"      🔧 {call['name']}({args})")
            elif node == "supervisor":
                d, s = data["decision"], data["sufficiency"]
                print(f"  🧭 supervisor → {d['next']}  (step {data['step_count']}, 누락 관점: {s['missing'] or '없음'})")
                print(f"      사유: {d['reason']}" + (f"\n      ⚠ 가드: {d['override']}" if d["override"] else ""))
            elif node in ("researcher", "analyst"):
                if data.get("node_status", {}).get(node) == "failed":
                    print(f"  ❌ {node} 실패: {data.get('last_error')}")
                else:
                    f = data["findings"][node]
                    print(f"  ✅ {node} 완료 (시도 {f['attempt']}, 관점 {list(f['coverage'])}, 출처 {f['source_ids']})")
            elif node == "writer":
                print(f"  ✍️  writer 리포트 작성 ({len(data['report'])}자)")
            elif node == "evaluator":
                ev = data["eval_result"]
                print(f"  🔍 evaluator {'통과' if ev['passed'] else '미달'}"
                      + ("" if ev["passed"] else "\n" + "\n".join(f"      - {i}" for i in ev["rule_issues"][:5])))

    state = graph.get_state(config).values
    report = state.get("report")
    if report:
        print("\n" + "=" * 70 + "\n" + report + "\n" + "=" * 70)
        print(f"\n📄 리포트 저장: {save_report(report, trace_id)}")


def main() -> None:
    graph = build_graph()
    if sys.argv[1:] == ["--graph"]:
        out = Path(__file__).parent / "docs" / "images" / "graph.png"
        out.write_bytes(graph.get_graph().draw_mermaid_png())
        print(f"그래프 이미지 저장: {out}")
        return

    thread_id = str(uuid.uuid4())
    if len(sys.argv) > 1:
        run(graph, " ".join(sys.argv[1:]), thread_id)
        return

    print("💰 재무 분석 agent (종료: exit)")
    while True:
        try:
            q = input("\n질문> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in {"exit", "quit", "q"}:
            break
        if q:
            run(graph, q, thread_id)


if __name__ == "__main__":
    main()
