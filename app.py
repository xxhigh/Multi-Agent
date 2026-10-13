"""재무 분석 멀티 agent 실행 스크립트 (CLI).

사용법 (프로젝트 루트에서):
    python app.py                         # 대화형 모드
    python app.py "셀트리온 재무 분석해줘"   # 단일 질문

리포트는 outputs/ 에 저장된다.
"""

import sys
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from agents import build_graph  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"


def _save_report(report: str) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"report_{datetime.now():%Y%m%d_%H%M%S}.md"
    path.write_text(report, encoding="utf-8")
    return path


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
        print(f"\n📄 리포트 저장: {_save_report(report)}")


def main() -> None:
    # InMemorySaver를 공유하므로 같은 thread_id로 후속 질문을 이어갈 수 있다.
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
