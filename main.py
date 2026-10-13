"""재무 분석 멀티 agent CLI.

사용법:
    python main.py                        # 대화형 모드
    python main.py "엔비디아 재무 분석해줘"   # 단일 질문
"""

import sys
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from finance_agent import build_graph  # noqa: E402

REPORT_DIR = Path(__file__).parent / "reports"


def run(graph, question: str, thread_id: str) -> None:
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 50}
    report = None
    stream = graph.stream({"messages": [("user", question)]}, config, stream_mode="updates", subgraphs=True)
    for namespace, update in stream:
        for node, data in update.items():
            if namespace:  # researcher/analyst 내부의 도구 호출 표시
                if node == "agent" and data and data["messages"][-1].tool_calls:
                    for call in data["messages"][-1].tool_calls:
                        args = ", ".join(f"{k}={v}" for k, v in call["args"].items())
                        print(f"      🔧 {call['name']}({args})")
            elif node == "supervisor":
                nxt = data.get("next")
                print(f"  🧭 supervisor → {nxt}")
            elif data and data.get("messages"):
                msg = data["messages"][-1]
                print(f"  ✅ {node} 완료 ({len(msg.content)}자)")
                if node == "writer":
                    report = msg.content

    if report:
        print("\n" + "=" * 70 + "\n" + report + "\n" + "=" * 70)
        REPORT_DIR.mkdir(exist_ok=True)
        path = REPORT_DIR / f"report_{datetime.now():%Y%m%d_%H%M%S}.md"
        path.write_text(report, encoding="utf-8")
        print(f"\n📄 리포트 저장: {path}")


def main() -> None:
    graph = build_graph()
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
