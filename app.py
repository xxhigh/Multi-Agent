"""AI 스타트업 투자 평가 멀티 agent 실행 스크립트 (CLI).

사용법 (프로젝트 루트에서):
    python app.py                                  # 대화형 모드
    python app.py "Figure AI 투자 가능성 평가해줘"   # 단일 질문

보고서는 outputs/ 에 PDF(와 원본 Markdown)로, 실행 트레이스(결정·사유·작업 결과)는 outputs/traces/<run_id>.jsonl 에 저장된다.
"""

import sys
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from agents import build_graph, pdf, trace  # noqa: E402
from agents.report import MAX_PAGES  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"


def _save_report(report: str) -> Path:
    """보고서를 PDF로 저장하고 PDF 경로를 돌려준다. 원본 Markdown도 같은 이름으로 함께 남긴다."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"report_{datetime.now():%Y%m%d_%H%M%S}.pdf"
    path.with_suffix(".md").write_text(report, encoding="utf-8")
    pdf.to_pdf(report, path)
    pages = pdf.page_count(report)
    if pages > MAX_PAGES:
        print(f"\n⚠️  보고서가 {pages}쪽입니다 (상한 {MAX_PAGES}쪽).")
    return path


def run(graph, question: str | None, thread_id: str) -> bool:
    """질문을 실행한다. question이 None이면 중단된 실행을 마지막 체크포인트에서 재개한다. 성공하면 True."""
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 50}
    payload = {"messages": [("user", question)]} if question is not None else None
    report, status = None, None
    try:
        for namespace, update in graph.stream(payload, config, stream_mode="updates", subgraphs=True):
            for node, data in update.items():
                if not data:
                    continue
                if namespace:  # worker 내부의 도구 호출 표시
                    if node == "agent" and data["messages"][-1].tool_calls:
                        agent = namespace[0].split(":")[0]
                        for call in data["messages"][-1].tool_calls:
                            args = ", ".join(f"{k}={v}" for k, v in call["args"].items())
                            print(f"      🔧 [{agent}] {call['name']}({args})")
                elif node == "init":
                    print(f"  🆔 run_id={data['run_id']}")
                elif node == "supervisor":
                    status = data.get("status", status)
                    decision = data.get("decision")
                    if decision:
                        agents = ", ".join(t["agent"] + (" (재시도)" if t["attempt"] > 1 else "") for t in decision["tasks"])
                        missing = f" (근거 부족: {', '.join(decision['missing'])})" if decision["missing"] else ""
                        print(f"  🧭 [{decision['step']}] supervisor → {agents}{missing}\n       사유: {decision['reason']}")
                    else:
                        print(f"  🏁 supervisor → 종료 ({status})")
                elif node == "quality":
                    q = data["quality"]
                    marks = {True: "✅", False: "❌", None: "–"}
                    summary = " / ".join(f"{item} {marks[ok]}" for item, ok in q["items"].items())
                    rewrite = f" → 다시 작성 ({data['quality_rounds']}회차)" if "quality_rounds" in data else ""
                    print(f"  🔍 품질 평가: {summary}{rewrite}")
                    for issue in q["issues"]:
                        print(f"       {issue[:200]}")
                elif data.get("errors"):
                    for e in data["errors"]:
                        print(f"  ⚠️  {node} 실패 (시도 {e['attempt']}): {e['error']}")
                elif data.get("report"):
                    report = data["report"]
                    print(f"  ✅ {node} 완료 ({len(report)}자)")
                elif data.get("findings"):
                    print(f"  ✅ {node} 완료 ({len(data['findings'][-1]['content'])}자)")
    except Exception as e:  # noqa: BLE001 - 진행 상태는 체크포인트에 남아 있으므로 재개할 수 있다
        print(f"\n❌ 실행 중단: {type(e).__name__}: {e}")
        return False

    values = graph.get_state(config).values
    run_id = values["run_id"]
    report = values.get("report") or report  # 품질 평가에서 REFERENCE 를 정리한 최종본
    if report and values.get("quality") and not values["quality"]["passed"]:
        print("\n⚠️  품질 평가에 미달한 항목이 남아 있습니다 (다시 쓰기 한도 도달).")
    if report:
        print("\n" + "=" * 70 + "\n" + report + "\n" + "=" * 70)
        path = _save_report(report)
        trace.log(run_id, "report_saved", path=str(path))
        print(f"\n📄 보고서 저장(PDF): {path}")
    elif status == "failed":
        print("\n❌ 보고서를 작성하지 못했습니다.")
    print(f"🔎 트레이스: {trace.path(run_id)}")
    return status == "done"


def main() -> None:
    # InMemorySaver를 공유하므로 같은 thread_id로 후속 질문을 이어가거나 중단된 실행을 재개할 수 있다.
    graph = build_graph()
    thread_id = str(uuid.uuid4())

    if len(sys.argv) > 1:
        sys.exit(0 if run(graph, " ".join(sys.argv[1:]), thread_id) else 1)

    print("🤖 AI 스타트업 투자 평가 agent — Physical AI / Robotics (종료: exit, 중단된 실행 재개: resume)")
    while True:
        try:
            q = input("\n질문> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in {"exit", "quit", "q"}:
            break
        if q.lower() in {"resume", "재개"}:
            if graph.get_state({"configurable": {"thread_id": thread_id}}).next:
                run(graph, None, thread_id)
            else:
                print("재개할 실행이 없습니다.")
        elif q and not run(graph, q, thread_id):
            if graph.get_state({"configurable": {"thread_id": thread_id}}).next:
                print("resume 을 입력하면 중단된 지점부터 이어서 실행합니다.")


if __name__ == "__main__":
    main()
