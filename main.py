"""재무 분석 Supervisor 멀티 에이전트 CLI.

사용법:
    python main.py                          # 대화형 모드
    python main.py "셀트리온 재무 분석해줘"    # 단일 질문

산출물:
    outputs/reports/report_{trace_id}.md          최종 보고서
    outputs/runs/{trace_id}/decisions.jsonl       supervisor 라우팅·평가 결정과 사유
    outputs/runs/{trace_id}/evidence/E*.md        근거 원문
    outputs/runs/{trace_id}/run-summary.json      라우팅 순서·재작업·수정 횟수 요약
"""

import sys

from dotenv import load_dotenv

load_dotenv()

from finance_agent import build_graph  # noqa: E402
from finance_agent.runner import new_run, save_outputs  # noqa: E402


def run(graph, question: str) -> None:
    trace_id, state, cfg = new_run(question)
    print(f"🔎 trace_id = {trace_id}")
    for namespace, update in graph.stream(state, cfg, stream_mode="updates", subgraphs=True):
        for node, data in update.items():
            if not data:
                continue
            if namespace:  # researcher/analyst 내부의 도구 호출 표시
                msgs = data.get("messages") or []
                if node == "agent" and msgs and msgs[-1].tool_calls:
                    for call in msgs[-1].tool_calls:
                        args = ", ".join(f"{k}={v}" for k, v in call["args"].items())
                        print(f"      🔧 {call['name']}({args})")
            elif node == "supervisor":
                targets = data.get("target_perspectives") or []
                print(f"  🧭 supervisor → {data.get('next')}" + (f"  {targets}" if targets else ""))
            elif node == "evaluator" and data.get("eval_result"):
                v = data["eval_result"]
                print(f"  🧪 evaluator: {'PASS' if v.passed else 'FAIL'}"
                      + ("" if v.passed else f" → {v.fix_target}"))
            else:
                print(f"  ✅ {node} 완료")

    final = graph.get_state(cfg).values
    paths = save_outputs(trace_id, final)
    if final.get("report"):
        print("\n" + "=" * 70 + "\n" + final["report"] + "\n" + "=" * 70)
    print(f"\n상태: {final.get('status')} | supervisor 판단 {final.get('step_count')}회 | "
          f"수정 {final.get('revision_count')}회 | 관점별 작업 {final.get('attempts')}")
    for kind, path in paths.items():
        print(f"📄 {kind}: {path}")


def main() -> None:
    graph = build_graph()
    if len(sys.argv) > 1:
        run(graph, " ".join(sys.argv[1:]))
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
            run(graph, q)


if __name__ == "__main__":
    main()
